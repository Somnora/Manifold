"""Desktop entrypoint: the whole product as one process.

This is what PyInstaller freezes into the sidecar binary the Tauri shell
spawns (and what a double-click runs standalone). It boots the same app
factory as development, binds strictly to loopback, and serves the bundled
dashboard at /.

`manifold-backend --mcp` runs the MCP stdio bridge instead of the server,
so an MCP client (Claude Desktop, Claude Code) can drive Manifold with
ONLY the installed app - no dev checkout, no uv. The bridge is the same
HTTP-only thin client as `uv run manifold-mcp`; it needs the app (or any
backend) already running to answer.

`manifold-backend --doctor` prints the end-to-end wiring checklist
(backend up? token accepted? registered in which agent configs, at what
scope?) and exits nonzero when an agent would be blocked. See app/doctor.py.

MANIFOLD_MOCK=1 works here exactly as in development - the packaged app can
be demoed with zero credentials and zero spend.
"""

from __future__ import annotations

import os
import sys
import threading

import uvicorn

from app.main import create_default_app

HOST = "127.0.0.1"
PORT = int(os.environ.get("MANIFOLD_PORT", "8000"))


def _watch_parent() -> None:
    """Exit when the shell that spawned us dies.

    PyInstaller --onefile runs as bootloader -> real process; the Tauri
    shell can only kill the bootloader, which would orphan this process
    and leave :8000 held forever (found live at the Phase 28 gate). Our
    stdin is a pipe from the shell, so EOF on it means the shell is gone
    - the reliable cross-platform death signal. Opt-in via env so a
    terminal run (stdin may be closed or a TTY) never self-terminates.
    """
    def watch() -> None:
        try:
            sys.stdin.buffer.read()   # blocks until the pipe closes
        except Exception:
            pass
        # Leave a tombstone BEFORE os._exit, which bypasses the FastAPI
        # lifespan, atexit and every finally in the process. Without this
        # line a normal Cmd-Q is indistinguishable from a crash in the
        # record - which is exactly how a 331-second quit on 2026-08-16
        # was investigated for hours as a silent crash. Best-effort and
        # never raises: a tombstone must not be able to wedge a shutdown.
        try:
            from app.config import DATA_ROOT
            from app.liveness import record_stop
            record_stop(DATA_ROOT / "manifold.db", "shell gone (app quit)")
        except Exception:   # noqa: BLE001
            pass
        print("manifold: shell gone (stdin EOF); shutting down", flush=True)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def run_mcp() -> None:
    """Run the MCP stdio bridge in place of the server.

    stdin/stdout ARE the MCP protocol channel here, so two desktop-mode
    behaviors must not run: the parent watchdog (it reads stdin and would
    eat protocol frames) and the startup banner (a stray stdout line breaks
    the client's JSON-RPC parse). The bridge talks to whatever backend is
    listening on MANIFOLD_PORT - normally the running desktop app.
    """
    os.environ.setdefault("MANIFOLD_API_URL", f"http://{HOST}:{PORT}")
    # The bridge authenticates like every other client (Phase 78). MCP
    # clients spawn this binary with a clean env, so when the token is not
    # already provided, read it from the app's own .env - the file the
    # backend generated it into on first real-mode boot - instead of
    # requiring every MCP config to carry the secret by hand.
    if not os.environ.get("MANIFOLD_API_TOKEN"):
        from dotenv import dotenv_values

        from app.config import DATA_ROOT
        token = (dotenv_values(DATA_ROOT / ".env") or {}).get(
            "MANIFOLD_API_TOKEN") or ""
        if token:
            os.environ["MANIFOLD_API_TOKEN"] = token
    from app import mcp_server
    mcp_server.main()


def main() -> None:
    if "--mcp" in sys.argv[1:]:
        run_mcp()
        return
    if "--doctor" in sys.argv[1:]:
        from app import doctor
        doctor.main()
        return
    if os.environ.get("MANIFOLD_PARENT_WATCHDOG") == "1":
        _watch_parent()
    app = create_default_app()
    print(f"manifold: serving on http://{HOST}:{PORT}", flush=True)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    except SystemExit:
        raise
    except OSError as exc:
        # The one predictable failure: the port is taken. Say so plainly.
        print(f"manifold: cannot bind {HOST}:{PORT} ({exc}). "
              f"Set MANIFOLD_PORT to a free port and relaunch.",
              file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
