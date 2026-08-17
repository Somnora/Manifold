"""Is the backend there, and if not, say so out loud.

THE INCIDENT. On 2026-08-16 the desktop app stopped at 23:21:25 and came
back at 23:26:56 - 331 seconds. In that window a $1.99/hr A100 kept
billing, five MCP bridge processes got ECONNREFUSED, and another agent
session sat blocked. Nobody was told. It was found because that session
gave up and asked a human.

Then the diagnosis went wrong, which is the more interesting half. There
was no crash report, no jetsam record, the machine never slept, and the
log simply stopped mid-line with no shutdown marker - so it read exactly
like a silent crash, and hours went into hunting one. It was almost
certainly a normal quit: `desktop.py`'s parent watchdog calls
`os._exit(0)`, which bypasses the FastAPI lifespan, and its one message
goes to stdout rather than the log. A DELIBERATE QUIT AND A SILENT CRASH
ARE INDISTINGUISHABLE IN THE RECORD. That is the defect. Not the stopping
- the app is allowed to stop - but that stopping leaves no trace and
announces nothing while paid work depends on it.

WHY THIS REPORTS AND NEVER RESTARTS. An auto-restarter was the obvious
fix and it is the wrong one, for three reasons found by attacking it:

  - It would have papered over someone pressing Cmd-Q. The common case is
    a normal quit; a supervisor that fights the user is a bug.
  - Restarting reseeds `dispatcher.last_activity` (dispatcher.py, the
    `setdefault` in the idle sweep). A restart loop therefore holds the
    idle countdown at zero forever and silently disables idle
    auto-termination - the product's central money guard, switched off by
    a safety feature.
  - A supervisor that quietly revives a crashing backend converts a loud
    failure into a quiet one, which is the direction this whole codebase
    spends its effort travelling away from.

So: this module observes, classifies, and says. It never starts, stops,
kills or restarts anything, and it imports nothing that could - no
orchestrator, no lambda_api, no connections, no Database (an AST test
enforces that, mirroring the one over mcp_server.py). The strongest thing
it does is print a sentence and raise a notification.

WEDGED IS NOT DEAD. The log carries nine `event_loop_blocked` warnings,
worst 4.4s. A process that is alive and slow must never be treated as one
that is gone, so the thresholds are calibrated against that measured
distribution rather than guessed: 2s marks "lagging" just above the worst
observed stall, and 10s sits far enough beyond it that a WEDGED verdict
means a real wedge. Both are reported. Neither is acted on.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("manifold.liveness")

# States, named once so the log line, the doctor line and the notification
# body cannot drift apart.
UP = "up"
LAGGING = "lagging"
WEDGED = "wedged"
APP_GONE = "app_gone"
BACKEND_DIED = "backend_died"
UNKNOWN = "unknown"

# Above the worst stall ever recorded here (4.4s), so a WEDGED verdict is a
# wedge and not a slow moment.
WEDGE_SECONDS = 10.0
# Just above the top of the observed stall distribution.
SLOW_SECONDS = 2.0

_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


@dataclass(frozen=True)
class Probe:
    """One observation. Carries no interpretation on purpose: what was seen
    and what it means are separated so the meaning can be unit-tested
    without a socket."""
    connected: bool
    status: int | None
    elapsed: float
    error: str | None = None        # "refused" | "timeout" | "os-error"


@dataclass(frozen=True)
class Verdict:
    state: str
    detail: str
    since: datetime | None = None


def probe_health(api_url: str, *, timeout: float = WEDGE_SECONDS,
                 clock=time.monotonic) -> Probe:
    """The only network call in this module. Never raises.

    A timeout is a FINDING, not an error: it is the difference between a
    backend that is gone and one that is wedged, which is the distinction
    this whole module exists to make.
    """
    import httpx
    started = clock()
    try:
        resp = httpx.get(f"{api_url.rstrip('/')}/health", timeout=timeout)
        return Probe(True, resp.status_code, clock() - started)
    except httpx.TimeoutException:
        return Probe(True, None, clock() - started, "timeout")
    except httpx.ConnectError:
        return Probe(False, None, clock() - started, "refused")
    except Exception:   # noqa: BLE001 - an observer must not crash
        return Probe(False, None, clock() - started, "os-error")


def shell_running(process_name: str = "manifold-desktop") -> bool | None:
    """Is the desktop shell alive? None when the question cannot be asked.

    This is what separates "the app was quit, and the backend went with it
    by design" from "the backend died on its own underneath a running app".
    Only the second is a malfunction, and until tonight nobody could tell
    which had happened.

    None (not False) where pgrep does not exist - Windows, or a stripped
    container. Answering False there would manufacture a BACKEND_DIED
    verdict out of an unaskable question.
    """
    if shutil.which("pgrep") is None:
        return None
    try:
        done = subprocess.run(["pgrep", "-x", process_name],
                              capture_output=True, timeout=5)
        return done.returncode == 0
    except Exception:   # noqa: BLE001
        return None


def last_log_timestamp(log_dir: Path) -> datetime | None:
    """When the backend last wrote anything, across the rotated set.

    Reads the tail of each file rather than all of it: manifold.log runs to
    several MB and this may be called on a poll. Rotation matters - the
    first pass at this incident missed manifold.log.1 entirely and drew the
    wrong conclusion from the half of the record it could see.
    """
    newest: datetime | None = None
    try:
        candidates = sorted(log_dir.glob("manifold.log*"))
    except OSError:
        return None
    for path in candidates:
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                back = min(fh.tell(), 65536)
                fh.seek(-back, os.SEEK_END)
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            match = _STAMP.match(line)
            if not match:
                continue
            try:
                stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if newest is None or stamp > newest:
                newest = stamp
            break
    return newest


def classify(probe: Probe, *, shell_alive: bool | None,
             slow_seconds: float = SLOW_SECONDS,
             wedge_seconds: float = WEDGE_SECONDS) -> str:
    """Pure. What the observation means, and nothing else.

    Order matters: answered-well, answered-slowly, answered-not-at-all,
    then the two ways of being absent. The absent cases are split by
    whether the SHELL is alive, because that is the only signal that
    separates a quit from a malfunction.
    """
    if probe.connected and probe.status == 200:
        return LAGGING if probe.elapsed >= slow_seconds else UP
    if probe.connected and probe.error == "timeout":
        return WEDGED
    if probe.connected and probe.status is not None:
        # Something answers on the port and it is not Manifold, or Manifold
        # is answering errors. Either way it is not healthy and not absent.
        return WEDGED
    if shell_alive is None:
        return UNKNOWN
    return BACKEND_DIED if shell_alive else APP_GONE


def describe(state: str, *, since: datetime | None = None,
             now: datetime | None = None, live_launches: int = 0) -> str:
    """Pure. The one sentence, used byte-identically by the log line, the
    doctor output and the notification body, so the three can never drift
    into telling the user different stories."""
    bits: list[str] = []
    if state == UP:
        bits.append("backend answering")
    elif state == LAGGING:
        bits.append("backend answering SLOWLY - it is alive but stalling; "
                    "check the log for event_loop_blocked")
    elif state == WEDGED:
        bits.append("backend is WEDGED: the port accepts a connection but "
                    "/health does not answer. It is running, so nothing "
                    "here will restart or kill it")
    elif state == APP_GONE:
        bits.append("the Manifold app is not running, so the backend went "
                    "down with it - by design, not a fault")
    elif state == BACKEND_DIED:
        bits.append("the app is running but its backend is GONE. That is a "
                    "malfunction, not a quit")
    else:
        bits.append("backend state could not be determined")

    if since is not None:
        ref = now or datetime.now()
        gap = (ref - since).total_seconds()
        if gap >= 0:
            bits.append(f"last log line {since.strftime('%H:%M:%S')} "
                        f"({_ago(gap)} ago)")
    if live_launches and state in (WEDGED, APP_GONE, BACKEND_DIED, UNKNOWN):
        # Never a dollar figure: live_launches() selects no rate column, and
        # inventing one would be the kind of confident-and-wrong number this
        # codebase keeps having to apologise for.
        bits.append(f"{live_launches} instance(s) still running on Lambda "
                    f"and still billing")
    return ". ".join(bits) + "."


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def count_live_launches(db_path: Path | str) -> int:
    """How many launches may still have a real, billing instance behind
    them. Read-only, via the existing helper - it opens the database
    `mode=ro` and never constructs Database, so it cannot run a migration
    against a live app's file."""
    try:
        from .db import live_launches
        return len(live_launches(str(db_path)))
    except Exception:   # noqa: BLE001
        return 0


def record_stop(db_path: Path | str, reason: str) -> bool:
    """Write the tombstone: one audit row saying this backend stopped, and
    why. Returns whether it was written.

    THE POINT. Without this, every clean quit is indistinguishable from a
    crash, which is exactly how tonight's five-minute quit consumed hours
    as a suspected crash. With it, the next boot can tell whether the
    previous run ended on purpose.

    Its own short-lived connection, deliberately: this runs from
    `os._exit(0)` paths where the app's Database may already be torn down,
    and from a process that must not be delayed. Never raises - a
    tombstone that could block a shutdown would be worse than no tombstone.
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute(
                "INSERT INTO audit_log (at, actor, action, detail) "
                "VALUES (?, 'backend', 'backend_stopped', ?)",
                (datetime.now(timezone.utc).isoformat(), reason),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:   # noqa: BLE001
        return False


def previous_run_ended_cleanly(db_path: Path | str) -> bool | None:
    """Did the run before this one stop on purpose?

    None when there is nothing to compare - a first boot, or a database
    predating the tombstone - because "we cannot tell" and "it crashed" are
    different answers and only one of them should wake anybody up. That
    distinction is the same one `busy: null` makes in the instances
    payload, and for the same reason.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT action FROM audit_log "
            "WHERE action IN ('backend_started', 'backend_stopped') "
            "ORDER BY id DESC LIMIT 2"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if not rows or rows[0]["action"] != "backend_started":
        return None          # no prior run recorded; nothing to judge
    if len(rows) < 2:
        return None          # the very first run to carry a marker
    return rows[1]["action"] == "backend_stopped"


def watch(*, api_url: str, data_root: Path, notify, interval: float = 30.0,
          sleep=time.sleep, iterations: int | None = None) -> None:
    """Poll, and speak ONLY when the answer changes.

    A watcher that reports every poll is a watcher whose output nobody
    reads, and this exists because a real outage went unnoticed. Reporting
    transitions keeps the signal rare enough to be believed.
    """
    previous: str | None = None
    count = 0
    while iterations is None or count < iterations:
        count += 1
        probe = probe_health(api_url)
        state = classify(probe, shell_alive=shell_running())
        if state != previous:
            notify(Verdict(
                state,
                describe(state,
                         since=last_log_timestamp(data_root / "logs"),
                         live_launches=count_live_launches(
                             data_root / "manifold.db")),
            ))
            previous = state
        if iterations is None or count < iterations:
            sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="manifold-watch",
        description="Report when Manifold's backend stops answering. "
                    "Reports only; never starts, stops or restarts it.")
    parser.add_argument("--url", default=os.environ.get(
        "MANIFOLD_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true",
                        help="print one verdict and exit")
    args = parser.parse_args()

    if args.data_dir:
        data_root = Path(args.data_dir).expanduser()
    else:
        from .config import DATA_ROOT
        data_root = DATA_ROOT

    def say(verdict: Verdict) -> None:
        mark = "OK  " if verdict.state == UP else "!!  "
        print(f"{mark}{datetime.now().strftime('%H:%M:%S')}  "
              f"{verdict.detail}", flush=True)

    if args.once:
        probe = probe_health(args.url)
        state = classify(probe, shell_alive=shell_running())
        say(Verdict(state, describe(
            state,
            since=last_log_timestamp(data_root / "logs"),
            live_launches=count_live_launches(data_root / "manifold.db"))))
        sys.exit(0 if state in (UP, LAGGING) else 1)

    print(f"watching {args.url} every {args.interval:.0f}s "
          f"(reports on change only; never restarts anything)", flush=True)
    try:
        watch(api_url=args.url, data_root=data_root, notify=say,
              interval=args.interval)
    except KeyboardInterrupt:
        pass
