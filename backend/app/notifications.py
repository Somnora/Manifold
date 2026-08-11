"""Notifications: the ping an unattended run owes you.

An approval gate that nobody hears about is not a safety feature, it is a
stall — the run sits paused, the GPU keeps billing, and the timeout
eventually auto-denies. Same for a job that failed at 3am. So every event
worth interrupting a person for lands here.

Two delivery channels, deliberately:

- IN-APP. Every notification is a row in the database, always, regardless of
  the toggles below (the toggles decide whether a *ping* fires, not whether
  history is kept). The dashboard polls the unread ones and raises a toast.
- OS. A real notification outside the window, so it reaches you while you are
  in another app — which is the whole point. Best-effort per platform, and it
  can never break a caller: a failed ping is logged and swallowed.

Nothing in this module can raise into the orchestrator, the dispatcher, or an
agent loop. A notification failing must never fail the work it describes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger("manifold.notifications")

# An OS notification is a courtesy, not a job: if the platform helper hangs,
# drop it rather than holding a lifecycle loop open.
OS_NOTIFY_TIMEOUT_SECONDS = 5.0


def _osascript_literal(text: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def os_notify(title: str, body: str) -> None:
    """Raise a real OS notification. Best-effort; never raises.

    Every argument is passed as an argv element (no shell), and on macOS the
    text is escaped into AppleScript string literals — a job name is
    attacker-adjacent data (it can carry a template's parameters), so it
    never becomes code.
    """
    try:
        if sys.platform == "darwin":
            script = (
                f"display notification {_osascript_literal(body)} "
                f"with title {_osascript_literal('Manifold')} "
                f"subtitle {_osascript_literal(title)}"
            )
            argv = ["osascript", "-e", script]
        elif os.name == "posix" and shutil.which("notify-send"):
            argv = ["notify-send", f"Manifold: {title}", body]
        else:
            return   # Windows/unsupported: the in-app toast still fires
        subprocess.run(
            argv, timeout=OS_NOTIFY_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except Exception:   # noqa: BLE001 - a ping must never break the work
        logger.debug("OS notification failed", exc_info=True)


class NotificationCenter:
    """Records notifications and (when enabled) pings the OS.

    `sender` is injected so tests and mock mode never touch the real
    Notification Center: mock mode passes a no-op, tests pass a recorder.
    """

    def __init__(self, db, prefs, *, sender=os_notify):
        self._db = db
        self._prefs = prefs           # PreferenceStore
        self._sender = sender

    def notify(self, kind: str, title: str, body: str = "",
               ref: str | None = None) -> str | None:
        """Record an event and ping if this kind is switched on.

        Returns the notification id, or None if the kind is switched off.
        Callers do not check the toggles themselves — they just report what
        happened and let the policy decide.
        """
        try:
            prefs = self._prefs.get().notifications
            if not prefs.wants(kind):
                return None
            notification_id = self._db.create_notification(
                kind=kind, title=title, body=body, ref=ref)
            if prefs.desktop:
                self._ping(title, body)
            return notification_id
        except Exception:   # noqa: BLE001
            logger.exception("failed to record notification %s", kind)
            return None

    def notify_once(self, kind: str, title: str, body: str = "",
                    ref: str | None = None) -> str | None:
        """notify(), but only the first time for this (kind, ref).

        For notifications that describe a CONDITION rather than an event: an
        idle instance is still idle on the next tick, and a caller that just
        calls notify() every tick turns the bell into noise. Returns None
        when the ping was already sent (or the kind is switched off).

        `ref` is required - without one there is nothing to dedupe on, and
        silently degrading to notify() would ship the every-tick bug the
        method exists to prevent.
        """
        if not ref:
            raise ValueError("notify_once needs a ref to dedupe on")
        try:
            if self._db.notification_exists(kind, ref):
                return None
        except Exception:   # noqa: BLE001 - a ping must never break the work
            logger.exception("failed to check notification history for %s", kind)
            return None
        return self.notify(kind, title, body, ref=ref)

    def _ping(self, title: str, body: str) -> None:
        """Fire the OS notification off the calling path.

        The event loop must not block on osascript, so when we are inside one
        the send goes to a thread; outside one (tests, sync callers) it just
        runs inline.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._sender(title, body)
            return
        loop.run_in_executor(None, self._sender, title, body)

    # -- read side (the dashboard bell) ---------------------------------------

    def list(self, *, unread_only: bool = False, limit: int = 50) -> list[dict]:
        return self._db.list_notifications(unread_only=unread_only, limit=limit)

    def unread_count(self) -> int:
        return self._db.unread_notification_count()

    def mark_read(self, ids: list[str] | None = None) -> int:
        return self._db.mark_notifications_read(ids)

    def clear(self) -> int:
        return self._db.clear_notifications()
