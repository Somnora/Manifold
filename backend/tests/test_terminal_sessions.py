"""Terminal sessions survive their WebSocket (the refresh-proof dock).

A socket drop (refresh, frozen tab) detaches; reconnecting with the same
?session= id reattaches to the SAME shell with scrollback replayed. An
explicit {"type": "close"} really ends the shell. All against the mock
shell - no GPU, no spend.
"""

import asyncio
import time

import pytest

from app.terminal_sessions import (
    FLOW_HIGH_WATER,
    FLOW_LOW_WATER,
    SCROLLBACK_CHARS,
    WS_SHELL_GONE,
    WS_TAKEN_OVER,
    TerminalSession,
    TerminalSessionManager,
)
from tests.test_terminal import launch_connected, read_until


# -- unit: TerminalSession -------------------------------------------------------

def make_session(session_id="local:t", **kw):
    calls = {"input": [], "resize": [], "closed": 0}
    session = TerminalSession(
        session_id,
        write_input=lambda d: calls["input"].append(d),
        resize=lambda c, r: calls["resize"].append((c, r)),
        close=lambda: calls.__setitem__("closed", calls["closed"] + 1),
        **kw,
    )
    return session, calls


class FakeWS:
    def __init__(self):
        self.sent: list[str] = []
        self.closed = False
        # The code we were closed WITH. The panel reconnects on an
        # unexplained close, so which code a close carries is behavior, not
        # decoration, and a fake that swallowed the argument would let a
        # missing one pass. (It did: this signature used to take no code,
        # so the real close raised TypeError inside a bare `except
        # Exception: pass` and the socket was never marked closed at all.)
        self.close_code: int | None = None

    async def send_text(self, text: str):
        if self.closed:
            raise RuntimeError("closed")
        self.sent.append(text)

    async def close(self, code: int | None = None):
        self.closed = True
        self.close_code = code


async def test_attach_replays_scrollback():
    session, _ = make_session()
    await session.feed("boot banner\r\n")
    await session.feed("$ ")
    ws = FakeWS()
    await session.attach(ws)
    assert ws.sent == ["boot banner\r\n$ "]
    # Attached: new output goes straight through.
    await session.feed("live")
    assert ws.sent[-1] == "live"


async def test_attach_does_not_drop_output_fed_during_replay():
    """Output arriving WHILE the scrollback replay is in flight must reach
    the new viewer. Binding the socket only after the (awaited) replay send
    lost exactly those chunks: recorded in scrollback, never delivered."""
    session, _ = make_session()
    await session.feed("history")
    gate = asyncio.Event()

    class SlowWS(FakeWS):
        async def send_text(self, text):
            await super().send_text(text)
            if text == "history":
                await gate.wait()          # the replay send parks here

    ws = SlowWS()
    attach_task = asyncio.create_task(session.attach(ws))
    while not ws.sent:                     # replay send submitted, parked
        await asyncio.sleep(0)
    await session.feed("live during replay")
    gate.set()
    await attach_task
    assert ws.sent == ["history", "live during replay"]


async def test_scrollback_is_bounded():
    session, _ = make_session()
    chunk = "x" * 10_000
    for _ in range(50):                      # 500k chars >> the 200k budget
        await session.feed(chunk)
    total = sum(len(c) for c in session._scrollback)
    assert total <= SCROLLBACK_CHARS


# -- flow control (freeze-under-heavy-output fix) --------------------------------

async def _feed_past_high_water(session):
    ws = FakeWS()
    await session.attach(ws)                 # resets flow; writable set
    chunk = "x" * 20_000
    while session._outstanding < FLOW_HIGH_WATER:
        await session.feed(chunk)
    return ws


async def test_flow_control_pauses_over_high_water_and_resumes_on_ack():
    session, _ = make_session()
    await _feed_past_high_water(session)
    # Browser hasn't acked and is now HIGH behind: the pump would pause.
    assert not session._writable.is_set()
    # It catches up (acks everything): writing resumes.
    session.ack(session._outstanding)
    assert session._writable.is_set()


async def test_partial_ack_stays_paused_until_below_low_water():
    session, _ = make_session()
    await _feed_past_high_water(session)
    over = session._outstanding
    session.ack(over - FLOW_LOW_WATER - 1)   # still just above LOW
    assert session._outstanding > FLOW_LOW_WATER
    assert not session._writable.is_set()
    session.ack(FLOW_LOW_WATER)              # now below LOW
    assert session._writable.is_set()


async def test_client_that_never_acked_does_not_wedge_the_shell(monkeypatch):
    # An old cached tab can't ack, so it must degrade to unpaced output
    # quickly rather than stall the shell: the short budget applies.
    monkeypatch.setattr("app.terminal_sessions.FLOW_WAIT_TIMEOUT", 0.01)
    session, _ = make_session()
    await _feed_past_high_water(session)
    assert session._acks_seen == 0
    assert not session._writable.is_set()
    await session.await_writable()           # returns despite no ack
    assert session._writable.is_set()


async def test_a_busy_acking_client_gets_the_long_budget(monkeypatch):
    # The bug this guards: a browser choking on render ALSO stops acking, so
    # a short timeout resumed the flood mid-choke and undid the backpressure.
    # Once a client has acked we know it speaks the protocol, so we wait.
    monkeypatch.setattr("app.terminal_sessions.FLOW_WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr("app.terminal_sessions.FLOW_BUSY_TIMEOUT", 5.0)
    session, _ = make_session()
    await _feed_past_high_water(session)
    session.ack(1)                           # proves it speaks flow control
    assert session._acks_seen == 1
    assert not session._writable.is_set()    # still way over HIGH
    # Must NOT resume on the short budget: waiting is the point.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(session.await_writable(), 0.05)
    assert not session._writable.is_set()


async def test_detach_clears_any_flow_pause():
    session, _ = make_session()
    ws = await _feed_past_high_water(session)
    assert not session._writable.is_set()
    session.detach(ws)                        # no browser -> never stay paused
    assert session._writable.is_set()
    assert session._outstanding == 0


async def test_reattach_resets_flow_accounting():
    session, _ = make_session()
    await _feed_past_high_water(session)
    assert not session._writable.is_set()
    fresh = FakeWS()
    await session.attach(fresh)              # a new viewer starts clean
    assert session._writable.is_set()
    assert session._outstanding == 0


async def test_second_attach_steals_the_session():
    session, _ = make_session()
    first, second = FakeWS(), FakeWS()
    await session.attach(first)
    await session.attach(second)
    assert first.closed
    # TAKEN_OVER, not a bare close. The panel reconnects on an unexplained
    # one, so without the code these two tabs would steal the session back
    # and forth from each other for as long as both stayed open.
    assert first.close_code == WS_TAKEN_OVER
    assert any("attached elsewhere" in t for t in first.sent)
    await session.feed("for the new tab")
    assert second.sent[-1] == "for the new tab"
    # The stale handler detaching must not detach the thief.
    session.detach(first)
    assert session.detached_at is None


async def test_detach_keeps_shell_and_marks_time():
    session, calls = make_session()
    ws = FakeWS()
    await session.attach(ws)
    session.detach(ws)
    assert calls["closed"] == 0              # shell still running
    assert session.detached_at is not None
    await session.feed("output while away")  # buffered, no crash
    ws2 = FakeWS()
    await session.attach(ws2)
    assert "output while away" in "".join(ws2.sent)


async def test_manager_reaps_only_past_grace():
    mgr = TerminalSessionManager(grace_seconds=60)
    fresh, _ = make_session("local:fresh")
    stale, stale_calls = make_session("local:stale")
    mgr.register(fresh)
    mgr.register(stale)
    stale.detached_at = time.monotonic() - 120   # long gone
    fresh.detached_at = time.monotonic() - 5     # a refresh in progress

    # One pass of the reap logic (avoid the 30s sleep).
    now = time.monotonic()
    for sid, s in list(mgr.sessions.items()):
        if s.detached_at is not None and now - s.detached_at > mgr.grace_seconds:
            mgr.kill(sid)

    assert "local:stale" not in mgr.sessions
    assert stale_calls["closed"] == 1
    assert "local:fresh" in mgr.sessions


async def test_manager_stop_kills_everything():
    mgr = TerminalSessionManager()
    session, calls = make_session()
    mgr.register(session)
    await mgr.stop()
    assert calls["closed"] == 1
    assert not mgr.sessions


# -- end to end over the real WS endpoints ---------------------------------------

def test_refresh_reattaches_same_instance_shell(client):
    """Drop the socket without a close (a refresh) and reconnect with the
    same session id: same shell, scrollback replayed, state intact."""
    instance_id = launch_connected(client)
    url = f"/instances/{instance_id}/terminal?session=tab1"

    with client.websocket_connect(url) as ws:
        read_until(ws, "$ ")
        for ch in "nvidia-smi\r":
            ws.send_json({"type": "input", "data": ch})
        read_until(ws, "$ ")
        # Context exit = bare socket close, exactly what a refresh does.

    ssh = client.app.state.orchestrator.connections[instance_id].ssh_connection()
    shells = [p for p in ssh.processes if p.command is None]
    assert len(shells) == 1                  # the shell survived the drop

    with client.websocket_connect(url) as ws:
        replay = ws.receive_text()           # scrollback, replayed on attach
        assert "mock shell" in replay        # the original banner...
        assert "NVIDIA-SMI" in replay        # ...and the output typed earlier
        # And it is live: the SAME shell keeps answering.
        for ch in "pwd\r":
            ws.send_json({"type": "input", "data": ch})
        read_until(ws, "$ ")

    shells = [p for p in ssh.processes if p.command is None]
    assert len(shells) == 1                  # still just one pty, reused


def test_explicit_close_really_ends_the_shell(client):
    instance_id = launch_connected(client)
    url = f"/instances/{instance_id}/terminal?session=tab2"
    with client.websocket_connect(url) as ws:
        read_until(ws, "$ ")
        ws.send_json({"type": "close"})
    assert client.app.state.terminal_sessions.get(
        f"inst:{instance_id}:tab2") is None

    # Reconnecting with the same id gets a FRESH shell (banner from scratch).
    with client.websocket_connect(url) as ws:
        banner = read_until(ws, "$ ")
        assert "mock shell" in banner
    ssh = client.app.state.orchestrator.connections[instance_id].ssh_connection()
    shells = [p for p in ssh.processes if p.command is None]
    assert len(shells) == 2                  # first closed, second opened


def test_no_session_id_keeps_ephemeral_behavior(client):
    """Old clients (no ?session=) get the pre-Phase-40 contract: the shell
    dies with the socket and nothing lingers in the registry."""
    instance_id = launch_connected(client)
    with client.websocket_connect(f"/instances/{instance_id}/terminal") as ws:
        read_until(ws, "$ ")
    assert client.app.state.terminal_sessions.sessions == {}


def test_two_session_ids_are_two_shells(client):
    instance_id = launch_connected(client)
    with client.websocket_connect(
            f"/instances/{instance_id}/terminal?session=a") as ws_a:
        read_until(ws_a, "$ ")
        with client.websocket_connect(
                f"/instances/{instance_id}/terminal?session=b") as ws_b:
            read_until(ws_b, "$ ")
            ssh = client.app.state.orchestrator.connections[
                instance_id].ssh_connection()
            shells = [p for p in ssh.processes if p.command is None]
            assert len(shells) == 2
