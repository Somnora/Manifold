"""Desktop entrypoint dispatch: `manifold-backend --mcp` runs the MCP bridge.

The frozen binary doubles as the MCP stdio server so an installed app is
enough to wire an agent up - no dev checkout. In --mcp mode stdin/stdout
are the PROTOCOL channel, so the parent watchdog (reads stdin) and the
startup banner (writes stdout) must never run there.
"""

import desktop
from app import mcp_server


def test_mcp_flag_routes_to_bridge(monkeypatch):
    called = []
    monkeypatch.setattr(mcp_server, "main", lambda: called.append("mcp"))
    monkeypatch.setattr(
        desktop.uvicorn, "run",
        lambda *a, **k: called.append("uvicorn"))
    monkeypatch.setattr(desktop.sys, "argv", ["manifold-backend", "--mcp"])
    # Phase 78: run_mcp falls back to reading MANIFOLD_API_TOKEN from the
    # real DATA_ROOT/.env when unset. Pre-set it so a test never touches
    # (or leaks) the developer's own .env.
    monkeypatch.setenv("MANIFOLD_API_TOKEN", "test-token-not-real")

    desktop.main()
    assert called == ["mcp"]


def test_mcp_mode_skips_the_stdin_watchdog(monkeypatch):
    """The watchdog reads stdin; in MCP mode that would eat protocol frames."""
    monkeypatch.setenv("MANIFOLD_PARENT_WATCHDOG", "1")
    monkeypatch.setenv("MANIFOLD_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(mcp_server, "main", lambda: None)
    watchdog = []
    monkeypatch.setattr(
        desktop, "_watch_parent", lambda: watchdog.append(True))
    monkeypatch.setattr(desktop.sys, "argv", ["manifold-backend", "--mcp"])

    desktop.main()
    assert watchdog == []


def test_mcp_mode_points_bridge_at_the_app_port(monkeypatch):
    """MANIFOLD_API_URL defaults to this app's own host:port, but an explicit
    value (bridging to a backend elsewhere) is never overridden."""
    monkeypatch.delenv("MANIFOLD_API_URL", raising=False)
    monkeypatch.setenv("MANIFOLD_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(mcp_server, "main", lambda: None)

    desktop.run_mcp()
    import os
    assert os.environ["MANIFOLD_API_URL"] == f"http://{desktop.HOST}:{desktop.PORT}"

    monkeypatch.setenv("MANIFOLD_API_URL", "http://127.0.0.1:9999")
    desktop.run_mcp()
    assert os.environ["MANIFOLD_API_URL"] == "http://127.0.0.1:9999"


def test_default_mode_still_serves(monkeypatch):
    served = []
    monkeypatch.setattr(
        desktop.uvicorn, "run", lambda *a, **k: served.append(True))
    monkeypatch.setattr(desktop.sys, "argv", ["manifold-backend"])
    # Stub the factory: this test pins the DISPATCH (default mode serves),
    # not app construction - and since Phase 78 a real create_default_app
    # would generate an API token into the developer's own .env.
    monkeypatch.setattr(desktop, "create_default_app", lambda: object())
    desktop.main()
    assert served == [True]
