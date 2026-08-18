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
    # uvicorn is imported lazily inside main()'s server branch (Phase
    # 106), so patch it at the source module - the late import resolves
    # to the same patched attribute.
    monkeypatch.setattr("uvicorn.run",
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
    monkeypatch.setattr("uvicorn.run",
                        lambda *a, **k: served.append(True))
    monkeypatch.setattr(desktop.sys, "argv", ["manifold-backend"])
    # Stub the factory: this test pins the DISPATCH (default mode serves),
    # not app construction - and since Phase 78 a real create_default_app
    # would generate an API token into the developer's own .env.
    monkeypatch.setattr("app.main.create_default_app", lambda: object())
    desktop.main()
    assert served == [True]


# -- Phase 106: the bridge stays thin at the ENTRY layer too ------------------


def test_desktop_entry_has_no_module_scope_server_imports():
    """The --mcp and --doctor paths pay for every module-scope import in
    desktop.py. The server graph (uvicorn, app.main -> fastapi, asyncssh,
    boto3) is ~2.5s of imports and ~76 dylib loads per spawn - the exact
    workload macOS re-assesses on fresh onefile extractions, which stalled
    a Claude Desktop handshake past its deadline. Server imports must stay
    inside main()'s server branch."""
    import ast
    from pathlib import Path

    import desktop

    tree = ast.parse(Path(desktop.__file__).read_text())
    module_imports: set[str] = set()
    for node in tree.body:   # module scope ONLY, not function bodies
        if isinstance(node, ast.Import):
            module_imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_imports.add(node.module or "")
    banned = {"uvicorn", "app.main", "app", "fastapi"}
    offending = {m for m in module_imports
                 if m in banned or m.startswith("app.")}
    assert not offending, (
        f"server-graph imports at desktop.py module scope: {offending}")


def test_bridge_parent_watch_exits_on_reparent():
    """SIGKILL on the onefile bootloader cannot reach the python child, so
    the bridge polls its parent and exits when the chain above it died."""
    import threading

    import desktop

    alive = [True, True, False]     # parent dies on the third poll
    exited = threading.Event()
    desktop._watch_bridge_parent(
        parent_alive=lambda: alive.pop(0) if alive else False,
        exit_fn=exited.set,
        poll_seconds=0.01,
    )
    assert exited.wait(timeout=2.0), "watch thread never called exit"


def test_bridge_parent_watch_stays_quiet_while_parent_lives():
    import time

    import desktop

    calls = []
    desktop._watch_bridge_parent(
        parent_alive=lambda: True,
        exit_fn=lambda: calls.append("exit"),
        poll_seconds=0.01,
    )
    time.sleep(0.1)
    assert calls == []
