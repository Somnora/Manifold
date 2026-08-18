"""Launch bootstrap: the setup script a launch hands its own box.

WHY THIS EXISTS. Every serious launch was followed by the same manual
dance - clone the repo, install the deps, pull the model, warm the cache -
and somebody had to be watching at the right moment to start it. An agent
that walked away came back to a billing GPU with nothing on it.

WHERE IT RUNS, and where it deliberately does not. Not cloud-init: that
fires before Manifold can observe anything, cannot be attached to an
adopted box, and is invisible until SSH comes up. Instead the script is
started through the EXISTING detached-command machinery (detached.py):
SFTP'd as bytes, launched under setsid, exit code recorded, liveness
probed, and - the part that matters - counted as activity, so the idle
sweep leaves a box alone while its bootstrap is still working.

HOW IT FIRES EXACTLY ONCE. Not at the moment the launch flips to active.
There are three separate places a launch becomes active (the normal
pipeline, the resume-after-restart path, and orphan repair), and at every
one of them the SSH connection is still CONNECTING - conn.run and
conn.sftp_write raise until it is CONNECTED. A hook at any of those sites
either fires twice or loses the start in a crash window.

So there is no hook. The dispatcher's telemetry loop RECONCILES instead
(Dispatcher._sweep_bootstrap), asking on every tick: is this launch
active, does it carry a script, is the connection CONNECTED, and does a
detached row with this launch's note NOT exist yet? Only then does it
start. The detached row is the marker, it lives in SQLite, and it is
checked before every start - so a restart, a repeated sweep, and all three
activation paths converge on one bootstrap. Waiting for CONNECTED happens
by itself: a tick that finds the connection down simply does nothing and
tries again.

WHAT A FAILED BOOTSTRAP DOES NOT DO. It does not terminate the box, and it
does not touch keep-alive. A typo in an apt line must not destroy work
already on the disk. A nonzero exit is reported loudly - in the instance
payload and in one notification - and the decision stays with the reader.
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger("manifold.bootstrap")

# A bootstrap is a setup script, not a program: 16 KiB is far above the
# clone-install-pull recipes this exists for, and the cap is named in the
# refusal so an oversized one gets an actionable 422 rather than a silent
# truncation. Matches detached.MAX_COMMAND_BYTES, which the same bytes
# have to fit through anyway.
MAX_BOOTSTRAP_BYTES = 16 * 1024

NOTE_PREFIX = "bootstrap:"


def note_for(launch_id: str) -> str:
    """The note this launch's bootstrap row carries, forever.

    This string IS the exactly-once mechanism. The sweep starts a bootstrap
    only when no detached row with this note exists, so the row created by
    a start is what stops every later sweep - across ticks, across backend
    restarts, and across all three paths by which a launch becomes active.
    It is a persistent marker rather than an in-memory flag for exactly
    that reason: a flag dies with the process, and the box does not.
    """
    return f"{NOTE_PREFIX}{launch_id}"


def is_bootstrap_note(note: str | None) -> bool:
    """Is this detached row a launch bootstrap? Used by the settle sites to
    decide whether a nonzero exit is worth a notification."""
    return bool(note) and note.startswith(NOTE_PREFIX)


def fingerprint(script: str) -> tuple[int, str]:
    """(bytes, short sha256) - everything the audit log is allowed to know.

    NOT the script. A bootstrap is precisely where someone writes `export
    HF_TOKEN=...` or a clone URL with a credential in it, and the audit log
    is a plain table every viewer can read. Size and hash answer "is this
    the script I meant" without echoing a secret, and the never-echo rule
    beats completeness.
    """
    raw = script.encode()
    return len(raw), hashlib.sha256(raw).hexdigest()[:12]


def audit_detail(*, instance_id: str, launch_id: str, handle: str, pid: int,
                 script: str) -> str:
    """The audit line for a started bootstrap: what, where, and how big -
    never what it says."""
    size, digest = fingerprint(script)
    return (f"{instance_id}: launch {launch_id} bootstrap started as "
            f"{handle} pid {pid} ({size} bytes, sha256 {digest}); "
            f"script content not logged")


def report(row: dict | None, *, connected: bool) -> dict | None:
    """The `bootstrap` field of an instance payload, or None for absent.

    Read straight off the detached row, with NO probe: this rides the
    hottest route in the app (the home page polls it every 2s) and an SSH
    round trip per instance per poll is the pile-up Phase 93 had to undo.

      settled row     -> exited (with its code), or vanished when no code
                         was recorded: it ended and HOW is not knowable,
                         which is never dressed up as an exit.
      open, offline   -> unreachable. A state of the CONNECTION; a box we
                         cannot see is never reported as having stopped.
      open, connected -> running.

    None when there is no row yet - the launch carries a script and the
    sweep has not reached it. That is none of the four states and is not
    invented into one; the field appears the moment the bootstrap starts.
    """
    if row is None:
        return None
    if row.get("exited_at"):
        exit_code = row.get("exit_code")
        if exit_code is None:
            return {"state": "vanished"}
        return {"state": "exited", "exit_code": exit_code}
    if not connected:
        return {"state": "unreachable"}
    return {"state": "running"}


def announce_exit(notifier, *, instance_id: str, handle: str,
                  note: str | None, exit_code: int | None) -> None:
    """One ping for a bootstrap that ended badly - from WHICHEVER settle
    site got there first.

    Two independent places settle a detached row: the telemetry loop's
    probe and the status-poll route. Either can be the one that sees the
    exit, and which one it is depends on whether anybody happened to be
    polling. Both call this, and notify_once keyed on the handle makes the
    second call a no-op - so the answer is one notification, never two and
    never zero.

    Only a NONZERO exit pings. A clean exit is the boring good case, and a
    vanished bootstrap (no exit code) means the box rebooted or something
    killed it - on Lambda a driver upgrade reboot is normal, and pinging on
    it would be noise about a thing that did not go wrong.

    Never raises: a notification failing must not break the settle that
    triggered it.
    """
    if notifier is None or not is_bootstrap_note(note):
        return
    if exit_code is None or exit_code == 0:
        return
    try:
        notifier.notify_once(
            "bootstrap_failed",
            f"Bootstrap script failed on {instance_id} (exit {exit_code})",
            f"The instance is still running and was NOT terminated: work "
            f"already on its disk is safe, and so is anything else you "
            f"started. Read the log at ~/.manifold/detached/{handle}.log on "
            f"the box, fix the script, and run the rest by hand.",
            ref=f"{NOTE_PREFIX}{handle}",
        )
    except Exception:   # noqa: BLE001 - a ping must never break the work
        logger.exception("bootstrap failure notification failed for %s",
                         handle)
