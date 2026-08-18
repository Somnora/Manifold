"""manifold doctor: is the product actually wired up, end to end?

Run as `manifold-backend --doctor` (installed app) or, from a dev
checkout's backend/, `uv run manifold-doctor`. It answers - in order -
the questions an agent (or its human) needs when "Manifold is open" and
"Manifold is connected to this session" disagree (the Phase 88 incident):

  1. Is a backend answering at MANIFOLD_API_URL? Mock or real?
  2. Does a token exist, and does the backend accept it? (Presence and
     status only - the value is never printed.)
  3. Which agent configs actually register manifold, and at WHAT SCOPE?
     A local-scope Claude Code entry is invisible from every other
     directory, which reads exactly like "not installed".
  4. Anything running right now?
  5. Is the discovery breadcrumb in place?
  6. Does the MCP bridge actually answer a client? The handshake check
     spawns the bridge exactly as a client would and speaks real
     JSON-RPC to it (initialize, then tools/list), solo and then two at
     once, with timings. Registered-but-times-out is the failure Claude
     Desktop showed on 2026-08-18, and checks 1-5 all pass through it.

Usage:
  manifold-backend --doctor                 every check, handshake last
  manifold-backend --doctor --no-handshake  skip the handshake (the
                                            slowest check, ~5-8s)
  manifold-backend --doctor --handshake     the handshake and nothing else

Like the MCP bridge, this is an outside-in client: it diagnoses over
HTTP and by reading config files, never by importing the orchestrator.
Prints a plain checklist; exits nonzero when an agent would be blocked.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import dotenv_values

from .breadcrumb import BREADCRUMB_DIR, register_command
from .config import DATA_ROOT


def default_api_url() -> str:
    port = os.environ.get("MANIFOLD_PORT", "8000")
    return os.environ.get("MANIFOLD_API_URL", f"http://127.0.0.1:{port}")


def resolve_token(data_root: Path) -> tuple[str, str]:
    """(token, source-for-the-report). Mirrors the bridge's own lookup:
    the environment first, then the backend's .env. The value is returned
    for the auth probe only and never lands in the report."""
    token = os.environ.get("MANIFOLD_API_TOKEN", "")
    if token:
        return token, "environment"
    env_file = data_root / ".env"
    token = (dotenv_values(env_file) or {}).get("MANIFOLD_API_TOKEN") or ""
    return token, str(env_file)


@dataclass
class Registration:
    client: str   # "claude code", "claude desktop", "codex", "gemini cli"
    scope: str    # "user", "local: <dir>", "project: <dir>", "global"

    def __str__(self) -> str:
        return f"{self.client} ({self.scope})"


def _json_or_none(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def scan_agent_configs(home: Path, cwd: Path) -> list[Registration]:
    """Every place a 'manifold' MCP entry can live, with its scope.

    Skips unreadable or malformed files silently: the doctor reports what
    IS registered, and a broken config shows up as an absence."""
    found: list[Registration] = []

    claude = _json_or_none(home / ".claude.json")
    if claude:
        if "manifold" in (claude.get("mcpServers") or {}):
            found.append(Registration("claude code", "user"))
        for proj_dir, proj in (claude.get("projects") or {}).items():
            if isinstance(proj, dict) and "manifold" in (proj.get("mcpServers") or {}):
                found.append(Registration("claude code", f"local: {proj_dir}"))

    project = _json_or_none(cwd / ".mcp.json")
    if project and "manifold" in (project.get("mcpServers") or {}):
        found.append(Registration("claude code", f"project: {cwd}"))

    desktop_cfg = _json_or_none(
        home / "Library" / "Application Support" / "Claude"
        / "claude_desktop_config.json")
    if desktop_cfg and "manifold" in (desktop_cfg.get("mcpServers") or {}):
        found.append(Registration("claude desktop", "global"))

    codex_path = home / ".codex" / "config.toml"
    try:
        codex = tomllib.loads(codex_path.read_text())
        if "manifold" in (codex.get("mcp_servers") or {}):
            found.append(Registration("codex", "global"))
    except (OSError, tomllib.TOMLDecodeError):
        pass

    gemini = _json_or_none(home / ".gemini" / "settings.json")
    if gemini and "manifold" in (gemini.get("mcpServers") or {}):
        found.append(Registration("gemini cli", "global"))

    return found


# -- the MCP handshake self-test ---------------------------------------------
#
# Phase 106. Claude Desktop showed "MCP manifold: Couldn't start this
# server ... Request timed out" while every other doctor check passed:
# the bridge WAS registered, the backend WAS up, and the client still
# killed the spawn before our `initialize` reply arrived. The only honest
# way to answer "does Manifold connect cleanly to a harness app?" is to
# be the harness app: spawn the same command the client is configured to
# spawn, speak the same JSON-RPC over the same pipes, and time it.

HANDSHAKE_DEADLINE_SECONDS = 15.0

# MCP's stdio transport is newline-delimited JSON-RPC (no framing
# headers). 2024-11-05 is the protocol revision the shipped clients
# negotiate; the bridge answers it today.
MCP_PROTOCOL_VERSION = "2024-11-05"

# What a reader should do about a slow or failed handshake. Kept as data
# so the report and the docs cannot drift apart.
HANDSHAKE_FAILURE_CONTEXT = (
    "slow first spawns of the unsigned onefile binary are usually macOS "
    "assessing a fresh extraction; a second run right after should be "
    "fast; if it stays slow, the fix commands follow"
)


@dataclass
class HandshakeProbe:
    """One spawn-and-handshake attempt.

    Every timing is None unless it was actually measured: a failed probe
    reports no tool count and no phase timing rather than a reassuring
    zero."""
    ok: bool
    initialize_ms: int | None = None
    tools_ms: int | None = None
    tool_count: int | None = None
    elapsed_ms: int | None = None
    error: str | None = None


class _ProbeFailed(Exception):
    """Internal: a probe gave up. The message is the report line."""


def bridge_spawn() -> tuple[list[str], str | None]:
    """The command an MCP client spawns for THIS install, and the working
    directory to spawn it in.

    Installed app: the frozen binary itself with --mcp, which is verbatim
    what the configs in docs/mcp-setup.md carry. Dev checkout: the
    manifold-mcp entry point (app.mcp_server:main, see pyproject) under
    this interpreter, from backend/ so `app` imports. `uv run` is
    deliberately not in the dev command: it would time the resolver, not
    the bridge."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--mcp"], None
    backend_dir = Path(__file__).resolve().parent.parent
    return ([sys.executable, "-c",
             "from app import mcp_server; mcp_server.main()"],
            str(backend_dir))


def _read_lines(stream, into: queue.Queue) -> None:
    """Pump the child's stdout into a queue; None marks EOF.

    A thread rather than select(): the same code then works on Windows,
    where select does not accept pipes."""
    try:
        for line in stream:
            into.put(line)
    except (OSError, ValueError):   # pipe closed under us by _kill_group
        pass
    finally:
        into.put(None)


def _kill_group(proc: subprocess.Popen) -> None:
    """End the whole process group, not just the process we spawned.

    A PyInstaller onefile binary is a bootloader that extracts itself and
    runs a SECOND process; killing only the bootloader leaves that child
    alive holding our pipes (watched it happen on 2026-08-18). The spawn
    used start_new_session, so the pair has a process group of its own
    and one killpg ends both."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:                       # Windows: no groups, kill the process
            proc.kill()
    except OSError:                 # already gone
        pass
    for pipe in (proc.stdin, proc.stdout):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _send(proc: subprocess.Popen, message: dict, what: str) -> None:
    try:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
    except (OSError, ValueError) as exc:
        raise _ProbeFailed(
            f"the bridge stopped reading before {what} "
            f"({exc.__class__.__name__}); it exited {proc.poll()}") from exc


def _await_reply(replies: queue.Queue, want_id: int, what: str,
                 deadline_at: float) -> dict:
    """The next JSON-RPC reply carrying `want_id`, or _ProbeFailed."""
    while True:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise _ProbeFailed(f"no {what} reply before the deadline")
        try:
            line = replies.get(timeout=min(remaining, 0.25))
        except queue.Empty:
            continue
        if line is None:
            raise _ProbeFailed(
                f"the bridge closed its output before answering {what}")
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            raise _ProbeFailed(
                f"the {what} reply was not JSON: {line[:120]!r}") from None
        if not isinstance(message, dict):
            raise _ProbeFailed(
                f"the {what} reply was not a JSON-RPC object: {line[:120]!r}")
        if message.get("id") != want_id:
            continue                # a notification or someone else's id
        if "error" in message:
            raise _ProbeFailed(f"{what} returned a JSON-RPC error: "
                               f"{message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def probe_handshake(
    command: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    deadline: float = HANDSHAKE_DEADLINE_SECONDS,
) -> HandshakeProbe:
    """Spawn `command` as an MCP client would and complete a handshake.

    initialize (timed from the spawn, because that is the whole wait a
    client measures against its own deadline), then the initialized
    notification, then tools/list. The child always dies with its process
    group before this returns, pass or fail."""
    started = time.monotonic()
    deadline_at = started + deadline
    stderr_file = tempfile.TemporaryFile(mode="w+b")

    def elapsed_ms() -> int:
        return round((time.monotonic() - started) * 1000)

    try:
        proc = subprocess.Popen(
            command, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=stderr_file, text=True, bufsize=1,
            start_new_session=True)      # its own group; see _kill_group
    except OSError as exc:
        stderr_file.close()
        return HandshakeProbe(
            ok=False, elapsed_ms=elapsed_ms(),
            error=f"could not spawn {command[0]}: {exc}")

    replies: queue.Queue = queue.Queue()
    threading.Thread(target=_read_lines, args=(proc.stdout, replies),
                     daemon=True).start()
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "manifold-doctor", "version": "1"},
            },
        }, "initialize")
        _await_reply(replies, 1, "initialize", deadline_at)
        initialize_ms = elapsed_ms()

        _send(proc, {"jsonrpc": "2.0",
                     "method": "notifications/initialized"}, "initialized")
        tools_started = time.monotonic()
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
              "tools/list")
        result = _await_reply(replies, 2, "tools/list", deadline_at)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise _ProbeFailed("the tools/list reply carried no tool list")
        return HandshakeProbe(
            ok=True,
            initialize_ms=initialize_ms,
            tools_ms=round((time.monotonic() - tools_started) * 1000),
            tool_count=len(tools),
            elapsed_ms=elapsed_ms(),
        )
    except _ProbeFailed as exc:
        detail = _stderr_tail(stderr_file)
        return HandshakeProbe(
            ok=False, elapsed_ms=elapsed_ms(),
            error=str(exc) + (f" [stderr: {detail}]" if detail else ""))
    finally:
        _kill_group(proc)
        stderr_file.close()


def _stderr_tail(stderr_file, limit: int = 200) -> str:
    """The child's last words, for a failure line. Never fatal."""
    try:
        stderr_file.flush()
        stderr_file.seek(0)
        text = stderr_file.read().decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""
    return text[-limit:].replace("\n", " ") if text else ""


def probe_handshake_concurrent(
    command: list[str], *, count: int = 2, **kwargs
) -> list[HandshakeProbe]:
    """`count` handshakes at the same moment.

    Claude Desktop does exactly this: a main copy and a shared-pool copy
    of the same server, spawned seconds apart. Two onefile bootloaders
    extracting at once is the case that timed out on 2026-08-18, so it is
    the case worth measuring."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(probe_handshake, command, **kwargs)
                   for _ in range(count)]
        return [future.result() for future in futures]


def _tool_count_phrase(probes: list[HandshakeProbe]) -> str:
    counts = [p.tool_count for p in probes]
    if len(set(counts)) == 1:
        return f"{counts[0]} tools each"
    return "tools: " + ", ".join(str(c) for c in counts)


def check_handshake(
    *,
    command: list[str] | None = None,
    cwd: str | None = None,
    deadline: float = HANDSHAKE_DEADLINE_SECONDS,
) -> tuple[list[str], bool]:
    """The handshake rows for the report, and whether they all passed.

    Three spawns: one solo, then two at once. Slow is not a FAIL here -
    only missing the deadline, dying, or answering with something that is
    not a JSON-RPC reply. The timings are printed either way, because
    "1.2s" and "11s" are different kinds of healthy."""
    if command is None:
        command, cwd = bridge_spawn()
    lines: list[str] = []
    ok = True

    def fail(line: str) -> None:
        nonlocal ok
        ok = False
        lines.append(f"  FAIL  {line}")

    def good(line: str) -> None:
        lines.append(f"  OK    {line}")

    def info(line: str) -> None:
        lines.append(f"  --    {line}")

    info("probing the command a client spawns: " + " ".join(command))

    solo = probe_handshake(command, cwd=cwd, deadline=deadline)
    if solo.ok:
        good(f"mcp handshake: initialize {solo.initialize_ms}ms, "
             f"tools/list {solo.tools_ms}ms ({solo.tool_count} tools)")
    else:
        fail(f"mcp handshake: {solo.error} "
             f"(gave up after {solo.elapsed_ms}ms, "
             f"deadline {round(deadline * 1000)}ms)")

    pair = probe_handshake_concurrent(command, cwd=cwd, deadline=deadline)
    if all(p.ok for p in pair):
        good(f"mcp handshake, {len(pair)} clients at once: initialize "
             + " and ".join(f"{p.initialize_ms}ms" for p in pair)
             + f" ({_tool_count_phrase(pair)})")
    else:
        for index, probe in enumerate(pair, start=1):
            if probe.ok:
                info(f"concurrent spawn {index} of {len(pair)}: initialize "
                     f"{probe.initialize_ms}ms ({probe.tool_count} tools)")
            else:
                fail(f"mcp handshake, {len(pair)} clients at once, spawn "
                     f"{index}: {probe.error} (gave up after "
                     f"{probe.elapsed_ms}ms, deadline "
                     f"{round(deadline * 1000)}ms)")

    if not ok:
        info(HANDSHAKE_FAILURE_CONTEXT + ":")
        info("  claude code:    " + register_command())
        info("  claude desktop: re-add manifold in ~/Library/Application "
             "Support/Claude/claude_desktop_config.json, then quit and "
             "reopen it")
        info("  the full walk-through is in docs/mcp-setup.md, "
             "\"Troubleshooting: the client says the server timed out\"")
    return lines, ok


def handshake_report(**kwargs) -> tuple[list[str], bool]:
    """`--doctor --handshake`: the handshake check on its own, with a
    header and a verdict, for when the client is the only suspect."""
    deadline = kwargs.get("deadline", HANDSHAKE_DEADLINE_SECONDS)
    lines = [f"manifold doctor: MCP handshake self-test "
             f"(deadline {round(deadline)}s per spawn)"]
    rows, ok = check_handshake(**kwargs)
    lines.extend(rows)
    lines.append(
        "  clean: a client that spawns this command gets the tools."
        if ok else
        "  BLOCKED: the handshake did not complete. A client would show "
        "its own timeout toast here.")
    return lines, ok


def diagnose(
    *,
    api_url: str | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
    data_root: Path | None = None,
    client: httpx.Client | None = None,
    handshake: bool = False,
) -> tuple[list[str], bool]:
    """Run every check; return (report lines, all-critical-checks-pass).

    Critical = backend answering, token accepted (or not required),
    manifold registered in at least one agent config, and (when
    `handshake` is on) the bridge completing a real MCP handshake: the
    failures that leave an agent blocked. Instance count and the
    breadcrumb are informational.

    `handshake` is off by default because it spawns three bridges and
    costs seconds; the CLI turns it on for every run that did not ask for
    --no-handshake."""
    api_url = api_url or default_api_url()
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    data_root = data_root or DATA_ROOT
    lines: list[str] = []
    ok = True

    def fail(line: str) -> None:
        nonlocal ok
        ok = False
        lines.append(f"  FAIL  {line}")

    def good(line: str) -> None:
        lines.append(f"  OK    {line}")

    def info(line: str) -> None:
        lines.append(f"  --    {line}")

    lines.append(f"manifold doctor  (backend expected at {api_url})")

    http = client or httpx.Client(base_url=api_url, timeout=5.0)
    try:
        # 1. backend up?
        backend_up = False
        mock = False
        try:
            resp = http.get("/health")
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                backend_up = True
                mock = bool(resp.json().get("mock"))
                good(f"backend answering at {api_url} "
                     f"({'mock/demo' if mock else 'real'} mode)")
            else:
                fail(f"something answers at {api_url} but it is not "
                     f"Manifold (/health returned {resp.status_code})")
        except httpx.HTTPError as exc:
            # "No backend" used to be the whole answer, and it is the least
            # useful half of one: it cannot tell a quit from a crash from a
            # wedge, so the reader's next step was guesswork. On 2026-08-16
            # that guesswork cost hours. Say WHICH kind of absent, and what
            # is still billing while it is.
            from .liveness import (APP_GONE, WEDGED, Probe, classify,
                                   count_live_launches, describe,
                                   last_log_timestamp, shell_running)
            state = classify(
                Probe(False, None, 0.0, "refused"),
                shell_alive=shell_running())
            fail(f"no backend at {api_url} ({exc.__class__.__name__}). "
                 + describe(state,
                            since=last_log_timestamp(data_root / "logs"),
                            live_launches=count_live_launches(
                                data_root / "manifold.db")))
            if state == APP_GONE:
                info("relaunch Manifold.app, or from a dev checkout: "
                     "uv run uvicorn app.main:create_default_app --factory")
            elif state == WEDGED:
                info("it is running, so do NOT kill it blindly: check "
                     "logs/manifold.log for event_loop_blocked first")
            else:
                info("start the Manifold app, or from a dev checkout: "
                     "uv run uvicorn app.main:create_default_app --factory")

        # 2. token present and accepted? (never the value)
        token, source = resolve_token(data_root)
        if not backend_up:
            info("token check skipped: no backend to test against "
                 + (f"(a token IS present, from {source})" if token
                    else "(and no token found either)"))
        else:
            try:
                probe = http.get("/instances", headers=(
                    {"Authorization": f"Bearer {token}"} if token else {}))
            except httpx.HTTPError as exc:
                probe = None
                fail(f"auth probe did not complete "
                     f"({exc.__class__.__name__})")
            if probe is not None and probe.status_code == 200:
                if token:
                    good(f"token from {source} ({len(token)} chars); "
                         f"the backend accepts it")
                else:
                    good("no token needed (backend is open"
                         + (", mock mode)" if mock else " on localhost)"))
                instances = probe.json().get("instances", [])
                names = ", ".join(
                    f"{i.get('name') or i.get('id')} ({i.get('status')})"
                    for i in instances[:5])
                info(f"instances: {len(instances)} known"
                     + (f" - {names}" if names else "")
                     + (" [mock fixtures]" if probe.json().get("mock") else ""))
            elif probe is not None and probe.status_code == 401:
                if token:
                    fail(f"backend rejected the token from {source} (401). "
                         f"Copy MANIFOLD_API_TOKEN from the .env the "
                         f"BACKEND actually loads: {data_root / '.env'}")
                else:
                    fail(f"backend requires a token and none was found. "
                         f"It lives in {data_root / '.env'} "
                         f"(MANIFOLD_API_TOKEN=...)")
            elif probe is not None:
                fail(f"auth probe got HTTP {probe.status_code} "
                     f"from /instances")
    finally:
        if client is None:
            http.close()

    # 3. registered anywhere?
    regs = scan_agent_configs(home, cwd)
    if regs:
        good("registered in: " + "; ".join(str(r) for r in regs))
        if all(r.scope.startswith(("local:", "project:")) for r in regs):
            info("every registration is directory-scoped: sessions "
                 "started ANYWHERE ELSE cannot see manifold. For "
                 "machine-wide access: " + register_command())
    else:
        fail("manifold is not registered in any agent config "
             "(claude code, claude desktop, codex, gemini cli). "
             "Fix: " + register_command())

    # 4. breadcrumb (informational)
    crumb = BREADCRUMB_DIR / "manifold.json"
    if crumb.exists():
        info(f"discovery breadcrumb present: {crumb}")
    else:
        info(f"no discovery breadcrumb at {crumb} "
             f"(written on next backend start)")

    # 5. the real MCP handshake, last: it is the slowest check (three
    # spawns) and the only one that costs seconds when everything is fine.
    if handshake:
        rows, handshake_ok = check_handshake()
        lines.extend(rows)
        ok = ok and handshake_ok

    lines.append("  all clear: an agent on this machine can find and drive "
                 "Manifold." if ok else
                 "  BLOCKED: fix the FAIL lines above, then re-run.")
    return lines, ok


def main() -> None:
    args = sys.argv[1:]
    if "--handshake" in args:
        lines, ok = handshake_report()
    else:
        lines, ok = diagnose(handshake="--no-handshake" not in args)
    print("\n".join(lines))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
