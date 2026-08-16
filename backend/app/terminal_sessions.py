"""Terminal sessions that survive their WebSocket.

The dock already keeps shells alive across page NAVIGATION (panels stay
mounted), but a browser refresh tears the whole page down: the WS drops,
and the shell died with it because the connection handler owned the
process. So a frozen tab + refresh meant setting up Claude in the
terminal all over again.

Now the process, its output pump, and a scrollback buffer live here,
keyed by a client-chosen session id. The dashboard reconnects with the
same id after a refresh, the scrollback is replayed, and the shell
carries on - whatever was running in it never noticed. A session ends
when:

- the user closes its tab (the client sends {"type": "close"}),
- the shell itself exits,
- nothing reattaches within the grace window (a tab that was closed
  outright, not refreshed - otherwise every closed tab would leak a
  live shell), or
- the backend shuts down.

This module is transport-glue only: it never decides WHAT may run a
shell (the endpoints in main.py keep their origin checks and the
managed-connection requirement).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

logger = logging.getLogger("manifold.terminal")

# ~ a few thousand lines of replay; xterm's own scrollback is 5000 lines.
SCROLLBACK_CHARS = 200_000

# Flow control watermarks (chars ~= bytes for terminal output). The browser
# acks what it has actually rendered; we stop feeding when it falls HIGH
# behind and resume at LOW, so a firehose or a full-screen TUI (Claude Code)
# can't outrun xterm's write buffer and freeze the tab. A client that never
# acks (an old cached tab) is covered by the wait timeout below.
FLOW_HIGH_WATER = 128 * 1024
FLOW_LOW_WATER = 16 * 1024
# How long to wait for a client that has NEVER acked: it probably predates
# flow control (an old cached tab), so give up quickly and stream unpaced.
FLOW_WAIT_TIMEOUT = 5.0
# How long to wait for a client that HAS acked. It speaks the protocol and is
# simply busy rendering — which is exactly when pausing matters, so this must
# be generous. (At 5s we resumed flooding mid-choke and undid the pause.)
FLOW_BUSY_TIMEOUT = 60.0


class TerminalSession:
    """One live shell + its recent output, attachable by at most one WS.

    The shell-specific mechanics (pty fd vs asyncssh process) are injected
    as callables, so local and instance shells share every behavior here.
    """

    def __init__(
        self,
        session_id: str,
        *,
        write_input: Callable[[str], None],
        resize: Callable[[int, int], None],
        close: Callable[[], None],
        on_output: Callable[[], None] | None = None,
    ):
        self.id = session_id
        self._write_input = write_input
        self._resize = resize
        self._close = close
        self._on_output = on_output      # e.g. idle-detection activity touch
        self._scrollback: list[str] = []
        self._scrollback_len = 0
        self._ws = None                  # the currently attached WebSocket
        self.exited = False
        # Born detached; the creating handler attaches right away.
        self.detached_at: float | None = time.monotonic()
        self.pump_task: asyncio.Task | None = None
        # Flow control: bytes sent to the browser but not yet acked as
        # rendered. `_writable` is clear while the browser is >HIGH behind.
        self._outstanding = 0
        self._writable = asyncio.Event()
        self._writable.set()
        # Has this client ever acked? Distinguishes "can't speak flow control"
        # from "speaks it but is busy" — they need opposite wait budgets.
        self._acks_seen = 0

    # -- flow control ----------------------------------------------------------

    async def await_writable(self) -> None:
        """Block while the attached browser is too far behind, so the producer
        (SSH channel / PTY) backpressures instead of overrunning xterm's write
        buffer.

        The wait budget depends on whether this client has EVER acked:
        - never acked -> it probably predates flow control; give up after
          FLOW_WAIT_TIMEOUT and stream unpaced (today's behavior, no stall).
        - has acked -> it speaks the protocol and is just busy rendering, so
          wait FLOW_BUSY_TIMEOUT. Timing out fast here would resume the flood
          mid-choke and defeat the whole mechanism; the long bound only exists
          so a browser that dies without closing its socket can't wedge the
          shell forever.
        """
        if self._writable.is_set():
            return
        budget = FLOW_BUSY_TIMEOUT if self._acks_seen else FLOW_WAIT_TIMEOUT
        try:
            await asyncio.wait_for(self._writable.wait(), budget)
        except asyncio.TimeoutError:
            logger.warning(
                "terminal %s: browser %s behind and silent for %.0fs; "
                "resuming unpaced", self.id,
                f"{self._outstanding} chars", budget)
            self._writable.set()

    def ack(self, rendered: int) -> None:
        """The browser reports `rendered` more chars actually drawn."""
        self._acks_seen += 1
        self._outstanding = max(0, self._outstanding - rendered)
        if self._outstanding <= FLOW_LOW_WATER:
            self._writable.set()

    # -- output path (called only by the session's pump task) -----------------

    async def feed(self, text: str) -> None:
        """Record output and forward it to the attached terminal, if any.

        Scrollback is recorded FIRST (so a reattach always replays the full
        output), then the send is paced by flow control — only the delivery to
        a slow browser waits, never the recording."""
        self._scrollback.append(text)
        self._scrollback_len += len(text)
        while (self._scrollback_len > SCROLLBACK_CHARS
               and len(self._scrollback) > 1):
            self._scrollback_len -= len(self._scrollback.pop(0))
        if self._on_output:
            self._on_output()
        ws = self._ws
        if ws is not None:
            try:
                await ws.send_text(text)
                # Count what the browser now owes an ack for; once it is HIGH
                # behind, the pump's next await_writable() pauses reading.
                self._outstanding += len(text)
                if self._outstanding >= FLOW_HIGH_WATER:
                    self._writable.clear()
            except Exception:
                # The browser vanished mid-send (refresh); keep the shell.
                self.detach(ws)

    async def mark_exited(self) -> None:
        """The shell ended on its own; tell whoever is watching."""
        self.exited = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.send_text("\r\n[manifold] shell exited\r\n")
                await ws.close()
            except Exception:
                pass

    # -- attach / detach -------------------------------------------------------

    async def attach(self, ws) -> None:
        """Adopt `ws` as this session's terminal, replaying the scrollback.
        A second tab attaching the same id steals the session (one keyboard
        per shell; the newest view wins)."""
        old, self._ws = self._ws, None
        if old is not None:
            try:
                await old.send_text(
                    "\r\n[manifold] this session was attached elsewhere\r\n")
                await old.close()
            except Exception:
                pass
        # Snapshot the scrollback and adopt the socket BEFORE the (awaited)
        # replay send. While that send is in flight the pump may feed() new
        # output; with `_ws` already set it goes to this socket, and frame
        # order is preserved because the replay frame was submitted first.
        # (Binding after the send lost exactly those chunks: recorded in
        # scrollback but never delivered to the new viewer.)
        replay = "".join(self._scrollback)
        self._ws = ws
        self.detached_at = None
        # Fresh viewer: reset flow accounting (the scrollback replay is a
        # one-shot bulk render, not something to pace or wait on). Its
        # protocol support is unknown again until it acks.
        self._outstanding = 0
        self._acks_seen = 0
        self._writable.set()
        if replay:
            try:
                await ws.send_text(replay)
            except Exception:
                # The browser vanished mid-attach (refresh); keep the shell.
                self.detach(ws)

    def detach(self, ws=None) -> None:
        """Let go of the terminal but keep the shell running. `ws` guards a
        stale handler (whose session was stolen) from detaching the thief."""
        if ws is not None and ws is not self._ws:
            return
        self._ws = None
        # No browser to pace to: never leave the pump blocked on flow control.
        self._outstanding = 0
        self._writable.set()
        self.detached_at = time.monotonic()

    # -- input path (called by the WS handler) ---------------------------------

    def tail_text(self, chars: int = 120) -> str:
        """The last `chars` of recorded output - the closest thing to
        "what was it doing" when a teardown has to escalate."""
        return "".join(self._scrollback)[-chars:]

    def write_input(self, data: str) -> None:
        self._write_input(data)

    def resize(self, cols: int, rows: int) -> None:
        self._resize(cols, rows)

    def kill(self) -> None:
        """End the shell for real (explicit close, reap, or shutdown)."""
        if self.pump_task is not None:
            self.pump_task.cancel()
        try:
            self._close()
        except Exception:
            pass


class TerminalSessionManager:
    """Registry + reaper. `grace_seconds` bounds how long a DETACHED session
    waits for a reattach. An attached session lives until it is closed or
    its shell exits.

    The reaper KILLS A SHELL, and a shell can have hours of an agent's work
    inside it. It used to do that on a 15-minute timer with nothing but a
    log line - no audit row, no notification - in a product that refuses to
    terminate an instance without first rescuing its files. The asymmetry
    was reported by the owner the honest way: "I lose my entire chat history
    with claude". The grace period is now long enough to survive stepping
    away (see config hub.terminal_grace_seconds), and `on_reap` exists so
    the one destructive thing this class does leaves a trace like every
    other destructive thing in Manifold.
    """

    def __init__(self, grace_seconds: float = 28800.0, on_reap=None):
        self.sessions: dict[str, TerminalSession] = {}
        self.grace_seconds = grace_seconds
        # on_reap(session, detached_seconds) -> None. Injected by main.py,
        # which owns the db and the notifier; this module stays free of
        # backend imports, like the rest of its dependencies.
        self._on_reap = on_reap
        self._reap_task: asyncio.Task | None = None

    def get(self, session_id: str) -> TerminalSession | None:
        session = self.sessions.get(session_id)
        if session is not None and session.exited:
            self.sessions.pop(session_id, None)
            return None
        return session

    def register(self, session: TerminalSession) -> None:
        self.sessions[session.id] = session

    def kill(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        session.kill()
        return True

    def start(self) -> None:
        self._reap_task = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        if self._reap_task is not None:
            self._reap_task.cancel()
            self._reap_task = None
        for session_id in list(self.sessions):
            self.kill(session_id)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for session_id, session in list(self.sessions.items()):
                if session.exited:
                    self.sessions.pop(session_id, None)
                elif (session.detached_at is not None
                      and now - session.detached_at > self.grace_seconds):
                    detached = now - session.detached_at
                    logger.info(
                        "reaping terminal session %s (detached %.0fs)",
                        session_id, detached,
                    )
                    # Report BEFORE killing: the callback reads the tail of
                    # what was on screen, and after kill() there is nothing
                    # left to say what died. A failing callback must never
                    # leave the shell running forever, so it cannot skip the
                    # kill below.
                    if self._on_reap is not None:
                        try:
                            self._on_reap(session, detached)
                        except Exception:
                            logger.exception(
                                "on_reap callback failed for %s", session_id)
                    self.kill(session_id)
