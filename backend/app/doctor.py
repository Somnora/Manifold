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

Like the MCP bridge, this is an outside-in client: it diagnoses over
HTTP and by reading config files, never by importing the orchestrator.
Prints a plain checklist; exits nonzero when an agent would be blocked.
"""

from __future__ import annotations

import json
import os
import sys
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


def diagnose(
    *,
    api_url: str | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
    data_root: Path | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[str], bool]:
    """Run every check; return (report lines, all-critical-checks-pass).

    Critical = backend answering, token accepted (or not required), and
    manifold registered in at least one agent config: the three failures
    that leave an agent blocked. Instance count and the breadcrumb are
    informational."""
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

    lines.append("  all clear: an agent on this machine can find and drive "
                 "Manifold." if ok else
                 "  BLOCKED: fix the FAIL lines above, then re-run.")
    return lines, ok


def main() -> None:
    lines, ok = diagnose()
    print("\n".join(lines))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
