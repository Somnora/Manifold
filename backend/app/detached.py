"""Detached commands: long work on an instance that outlives the request.

WHY THIS EXISTS. run_command is capped at 50 seconds so its response beats
the MCP client's ~60s timeout - and its own docstring then prescribed the
workaround: "start it detached (`nohup ... > log 2>&1 &`) and check the
log with a later run_command." A heavy user called that dance the single
most repeated boilerplate of their session, and twice misread the race
between a timeout and a file write as a dead box. The workaround they
resented was our documented advice. This module makes it a primitive.

THE SHAPE. Starting a command writes it VERBATIM to a script on the box
(over SFTP - the command is file bytes, never interpolated into a shell
line, so quoting is not a hazard and injection is not a category), then
launches it under setsid with a wrapper that records the exit code to a
file when it finishes. All state lives on the box (script, log, pid file,
exit file) plus one SQLite row here; nothing depends on this process
staying alive, so a backend restart changes nothing about a running
command - a property this codebase has now observed twice on the live
account (see DECISIONS.md, restart-invariance).

THE POINT BEYOND CONVENIENCE. A running detached command is EVIDENCE OF
WORK, and the telemetry loop probes it (dispatcher._probe_detached): a
live pid touches the activity clock and names itself in the instance's
activity verdict. That closes the documented idle-sweep gap for CPU/IO
work started through Manifold - the rsync and the cold-cache bootstrap
that read "idle" while doing exactly their job - without the agent
remembering to poll, set keep-alive, or hold anything in its memory. The
box protects itself because the work is visible, not because someone
recalls that it should be.

Four states, honestly distinguished (the truthful-or-absent rule):
  running     the pid is alive right now
  exited      the wrapper recorded an exit code
  vanished    no exit code AND the pid is gone - a reboot or a kill the
              wrapper never saw. NOT reported as exited: we do not know
              how it ended, only that it did.
  unreachable the instance cannot be probed. NOT a state of the command
              at all - "we cannot see" is never reported as "it stopped".
"""

from __future__ import annotations

import re
import uuid

# Everything lives under this directory on the instance. $HOME because the
# wrapper runs as the login user; ephemeral by design - a terminated box
# takes its detached state with it, and the SQLite row remains as history.
REMOTE_DIR = "$HOME/.manifold/detached"

# Handles are hex and generated here, so interpolating one into a shell
# line is safe by construction; the route re-validates with this anyway.
HANDLE_RE = re.compile(r"^d[0-9a-f]{11}$")

# Open handles per instance. Bounds the per-sample probe command and stops
# a loop from minting handles forever; far above any real use.
MAX_OPEN_PER_INSTANCE = 32

MAX_COMMAND_BYTES = 16 * 1024

# How much log the status endpoint returns. A tail, deliberately: the full
# log stays on the box and can be fetched with run_command/download_file.
LOG_TAIL_LINES = 60


def new_handle() -> str:
    return "d" + uuid.uuid4().hex[:11]


def script_path(handle: str) -> str:
    return f"{REMOTE_DIR}/{handle}.sh"


def script_sftp_path(handle: str) -> str:
    """The same file as script_path, for SFTP: no shell runs there, so
    $HOME would be a literal directory name. SFTP resolves relative paths
    against the login user's home - the same place the shell's $HOME
    points, which is what keeps the two views one file."""
    return f".manifold/detached/{handle}.sh"


def log_path(handle: str) -> str:
    return f"{REMOTE_DIR}/{handle}.log"


def launch_line(handle: str) -> str:
    """The one shell line that starts a written script detached.

    The user's command is ALREADY on the box as <handle>.sh (sent as raw
    bytes over SFTP); only the generated handle is interpolated here.
    setsid detaches from the SSH session so the connection closing cannot
    kill the work; </dev/null so nothing blocks on stdin; the wrapper
    writes the exit code to <handle>.exit as its last act, which is the
    difference between "exited 1" and "vanished" later.
    """
    d = REMOTE_DIR
    return (
        f'mkdir -p {d} && '
        f'setsid bash -c "bash {d}/{handle}.sh; '
        f'echo \\$? > {d}/{handle}.exit" '
        f'</dev/null >{d}/{handle}.log 2>&1 & '
        f'echo $! | tee {d}/{handle}.pid'
    )


def probe_line(handle: str) -> str:
    """One command that answers: how did/does <handle> stand, plus the log
    tail. Parsed by parse_probe; the sentinel keeps state and log apart."""
    d = REMOTE_DIR
    return (
        f'if [ -f {d}/{handle}.exit ]; then echo "EXIT $(cat {d}/{handle}.exit)"; '
        f'elif kill -0 "$(cat {d}/{handle}.pid 2>/dev/null)" 2>/dev/null; '
        f'then echo RUNNING; else echo VANISHED; fi; '
        f'echo ---MANIFOLD-LOG---; tail -n {LOG_TAIL_LINES} {d}/{handle}.log 2>/dev/null'
    )


def pids_alive_line(pid_by_handle: dict[str, int]) -> str:
    """One command that echoes the handle of every still-running pid.

    Fed to the telemetry loop's probe: N open handles cost one round trip,
    not N. Exit files are checked first so a finished command reports its
    real code even if the pid was recycled by something else.
    """
    d = REMOTE_DIR
    checks = [
        f'if [ ! -f {d}/{h}.exit ] && kill -0 {int(p)} 2>/dev/null; '
        f'then echo {h}; '
        f'elif [ -f {d}/{h}.exit ]; then echo "{h} EXIT $(cat {d}/{h}.exit)"; '
        f'else echo "{h} VANISHED"; fi'
        for h, p in pid_by_handle.items()
    ]
    return "; ".join(checks)


def parse_probe(stdout: str) -> tuple[str, int | None, str]:
    """(state, exit_code, log_tail) out of probe_line's output.

    Anything malformed is "unknown" - a parse failure must not be reported
    as any state of the command.
    """
    head, sep, tail = stdout.partition("---MANIFOLD-LOG---")
    log = tail.lstrip("\n") if sep else ""
    verdict = head.strip().splitlines()[-1].strip() if head.strip() else ""
    if verdict == "RUNNING":
        return "running", None, log
    if verdict == "VANISHED":
        return "vanished", None, log
    if verdict.startswith("EXIT"):
        try:
            return "exited", int(verdict.split()[1]), log
        except (IndexError, ValueError):
            return "unknown", None, log
    return "unknown", None, log


def parse_pids_alive(stdout: str) -> tuple[set[str], dict[str, int | None]]:
    """(handles still running, {handle: exit_code|None for settled ones}).

    A handle absent from BOTH is unknown this pass - reported as neither
    alive nor settled, and probed again next time. None as an exit code
    means "vanished": it ended, and how is not knowable.
    """
    alive: set[str] = set()
    settled: dict[str, int | None] = {}
    for line in stdout.splitlines():
        parts = line.strip().split()
        if not parts or not HANDLE_RE.match(parts[0]):
            continue
        if len(parts) == 1:
            alive.add(parts[0])
        elif parts[1] == "EXIT" and len(parts) >= 3:
            try:
                settled[parts[0]] = int(parts[2])
            except ValueError:
                continue
        elif parts[1] == "VANISHED":
            settled[parts[0]] = None
    return alive, settled
