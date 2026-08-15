"""The discovery breadcrumb: ~/.config/manifold/manifold.json.

Born from a real incident (Phase 88): an agent in ANOTHER repo was told
"Manifold is open for you to use", probed PATH and ~/.config, found
nothing under the word "manifold", and burned a working session (plus
idle-GPU dollars) on filesystem archaeology - while the app sat running
the whole time. Agents reflexively probe ~/.config/<product> early; this
file is the ten-second answer they were looking for.

Deliberate choices:
- ~/.config/manifold on EVERY platform, including macOS. Application
  Support is where the app's own state lives (DATA_ROOT); ~/.config is
  where agents look. The file exists for the reader, not the writer.
- Written on every backend boot, best-effort: a read-only home directory
  must never stop the backend. MANIFOLD_NO_BREADCRUMB=1 opts out.
- No secrets, ever. It says where the backend answers and how to register
  or diagnose it - the token stays in .env, exactly like everywhere else.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("manifold.breadcrumb")

BREADCRUMB_DIR = Path.home() / ".config" / "manifold"

_FROZEN = bool(getattr(sys, "frozen", False))


def _backend_dir() -> Path:
    """The dev checkout's backend/ directory (where pyproject lives)."""
    return Path(__file__).resolve().parent.parent


def register_command() -> str:
    """The one-line Claude Code registration for THIS install.

    --scope user is deliberate: the default (local) scope registers only
    for sessions started in the current directory, which is exactly the
    trap that caused the Phase 88 incident - "installed" and "visible to
    the agent in front of you" silently disagreeing.
    """
    if _FROZEN:
        return (f'claude mcp add manifold --scope user -- '
                f'"{sys.executable}" --mcp')
    return (f'claude mcp add manifold --scope user -- '
            f'uv run --directory "{_backend_dir()}" manifold-mcp')


def doctor_command() -> str:
    """How to self-diagnose THIS install (see app/doctor.py)."""
    if _FROZEN:
        return f'"{sys.executable}" --doctor'
    return f'uv run --directory "{_backend_dir()}" manifold-doctor'


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("manifold-backend")
    except Exception:
        return "unknown"


def breadcrumb_content(api_url: str) -> dict:
    """The full breadcrumb, as data. Pure enough to assert on in tests."""
    return {
        "what": (
            "Manifold - local orchestrator + dashboard for cloud GPU "
            "instances. One guarded backend; agents drive it over MCP."
        ),
        "api_url": api_url,
        "health_check": f"curl {api_url}/health",
        "agent_playbook": (
            f"curl {api_url}/skill  (the same document the MCP get_skill "
            f"tool returns)"
        ),
        "mcp_register": {
            "claude_code": register_command(),
            "other_clients": "see docs/mcp-setup.md in the Manifold repo "
                             "(Claude Desktop, Codex, Gemini CLI)",
        },
        "doctor": doctor_command(),
        "install": str(Path(sys.executable).resolve()) if _FROZEN
                   else str(_backend_dir().parent),
        "version": _version(),
        "pid": os.getpid(),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }


def write_breadcrumb(api_url: str, directory: Path | None = None) -> Path | None:
    """Write the breadcrumb; return its path, or None if skipped/unwritable.

    Never raises: discovery is a courtesy, and the backend booting matters
    more than a note about where it booted.
    """
    if os.environ.get("MANIFOLD_NO_BREADCRUMB") == "1":
        return None
    target_dir = directory if directory is not None else BREADCRUMB_DIR
    path = target_dir / "manifold.json"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(breadcrumb_content(api_url), indent=2) + "\n")
        return path
    except OSError as exc:
        logger.debug("breadcrumb not written to %s: %s", path, exc)
        return None
