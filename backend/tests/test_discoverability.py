"""Phase 88: agents (and users) can tell "installed" from "connected".

Born from a real incident: an agent in another repo was told "Manifold is
open for you to use", found nothing under the word "manifold" on PATH, in
~/.config, or in its MCP registry (the entry was directory-scoped), and
lost the session to filesystem archaeology while the app ran the whole
time. These tests pin the three answers we now give that agent: the
~/.config/manifold breadcrumb, the doctor's wiring checklist, and the
scope-aware scan of every agent config manifold can be registered in.
"""

import json

import httpx
import pytest

import desktop
from app import doctor
from app.breadcrumb import breadcrumb_content, register_command, write_breadcrumb
from app.doctor import Registration, diagnose, scan_agent_configs

SENTINEL_TOKEN = "sekret-token-do-not-print-123"


# -- breadcrumb --------------------------------------------------------------


def test_breadcrumb_written_and_self_describing(tmp_path, monkeypatch):
    monkeypatch.delenv("MANIFOLD_NO_BREADCRUMB", raising=False)
    path = write_breadcrumb("http://127.0.0.1:8000", directory=tmp_path)
    assert path == tmp_path / "manifold.json"
    data = json.loads(path.read_text())
    assert data["api_url"] == "http://127.0.0.1:8000"
    # The register command must carry --scope user: local scope is the
    # exact trap the incident fell into.
    assert "--scope user" in data["mcp_register"]["claude_code"]
    assert "claude mcp add manifold" in data["mcp_register"]["claude_code"]
    assert "doctor" in data


def test_breadcrumb_never_carries_a_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("MANIFOLD_API_TOKEN", SENTINEL_TOKEN)
    path = write_breadcrumb("http://127.0.0.1:8000", directory=tmp_path)
    assert SENTINEL_TOKEN not in path.read_text()


def test_breadcrumb_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("MANIFOLD_NO_BREADCRUMB", "1")
    assert write_breadcrumb("http://x", directory=tmp_path) is None
    assert not (tmp_path / "manifold.json").exists()


def test_breadcrumb_unwritable_dir_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("MANIFOLD_NO_BREADCRUMB", raising=False)
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o400)
    try:
        assert write_breadcrumb("http://x", directory=blocked / "sub") is None
    finally:
        blocked.chmod(0o700)


def test_breadcrumb_content_is_json_serializable():
    json.dumps(breadcrumb_content("http://127.0.0.1:8000"))


# -- agent-config scan --------------------------------------------------------


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_scan_finds_every_config_with_its_scope(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    _write(home / ".claude.json", {
        "mcpServers": {"manifold": {}},
        "projects": {"/some/dir": {"mcpServers": {"manifold": {}}},
                     "/other": {"mcpServers": {"obsidian": {}}}},
    })
    _write(cwd / ".mcp.json", {"mcpServers": {"manifold": {}}})
    _write(home / "Library" / "Application Support" / "Claude"
           / "claude_desktop_config.json",
           {"mcpServers": {"manifold": {}}})
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text('[mcp_servers.manifold]\ncommand = "x"\n')
    _write(home / ".gemini" / "settings.json", {"mcpServers": {"manifold": {}}})

    found = scan_agent_configs(home, cwd)
    scopes = {(r.client, r.scope) for r in found}
    assert scopes == {
        ("claude code", "user"),
        ("claude code", "local: /some/dir"),
        ("claude code", f"project: {cwd}"),
        ("claude desktop", "global"),
        ("codex", "global"),
        ("gemini cli", "global"),
    }


def test_scan_survives_malformed_and_missing_configs(tmp_path):
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    home.mkdir()
    (home / ".claude.json").write_text("{not json")
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text("also not [valid toml")
    assert scan_agent_configs(home, cwd) == []


# -- diagnose ------------------------------------------------------------------


def _fake_backend(token: str | None):
    """A MockTransport backend: /health up, /instances token-gated."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "mock": False})
        if request.url.path == "/instances":
            if token and request.headers.get("Authorization") != f"Bearer {token}":
                return httpx.Response(401, json={"detail": "bad token"})
            return httpx.Response(200, json={
                "instances": [{"id": "i-1", "name": "a10-ue5",
                               "status": "active"}],
                "mock": False,
            })
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="http://test")


@pytest.fixture
def registered_home(tmp_path):
    home = tmp_path / "home"
    _write(home / ".claude.json", {"mcpServers": {"manifold": {}}})
    return home


def test_diagnose_all_clear(tmp_path, registered_home, monkeypatch):
    monkeypatch.delenv("MANIFOLD_API_TOKEN", raising=False)
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / ".env").write_text(f"MANIFOLD_API_TOKEN={SENTINEL_TOKEN}\n")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    lines, ok = diagnose(api_url="http://test", home=registered_home,
                         cwd=cwd, data_root=data_root,
                         client=_fake_backend(SENTINEL_TOKEN))
    report = "\n".join(lines)
    assert ok
    assert "backend answering" in report
    assert "accepts it" in report
    assert "claude code (user)" in report
    assert "a10-ue5" in report
    # The credential is verified by status only, never echoed.
    assert SENTINEL_TOKEN not in report


def test_diagnose_backend_down_is_blocked(tmp_path, registered_home, monkeypatch):
    monkeypatch.delenv("MANIFOLD_API_TOKEN", raising=False)

    def refuse(request):
        raise httpx.ConnectError("refused")
    client = httpx.Client(transport=httpx.MockTransport(refuse),
                          base_url="http://test")
    lines, ok = diagnose(api_url="http://test", home=registered_home,
                         cwd=tmp_path, data_root=tmp_path, client=client)
    report = "\n".join(lines)
    assert not ok
    assert "no backend at http://test" in report
    assert "BLOCKED" in report


def test_diagnose_rejected_token_names_the_env_file(tmp_path, registered_home,
                                                    monkeypatch):
    monkeypatch.delenv("MANIFOLD_API_TOKEN", raising=False)
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / ".env").write_text("MANIFOLD_API_TOKEN=stale-token\n")

    lines, ok = diagnose(api_url="http://test", home=registered_home,
                         cwd=tmp_path, data_root=data_root,
                         client=_fake_backend(SENTINEL_TOKEN))
    report = "\n".join(lines)
    assert not ok
    assert str(data_root / ".env") in report
    assert "stale-token" not in report


def test_diagnose_unregistered_gives_the_exact_fix(tmp_path, monkeypatch):
    monkeypatch.delenv("MANIFOLD_API_TOKEN", raising=False)
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    lines, ok = diagnose(api_url="http://test", home=empty_home,
                         cwd=tmp_path, data_root=tmp_path,
                         client=_fake_backend(None))
    report = "\n".join(lines)
    assert not ok
    assert "not registered in any agent config" in report
    assert register_command() in report


def test_diagnose_warns_when_only_directory_scoped(tmp_path, monkeypatch):
    """The incident's exact shape: registered, but invisible from every
    other directory - the doctor must say so, not report all-clear."""
    monkeypatch.delenv("MANIFOLD_API_TOKEN", raising=False)
    home = tmp_path / "home"
    _write(home / ".claude.json", {
        "projects": {"/one/repo": {"mcpServers": {"manifold": {}}}}})
    lines, _ok = diagnose(api_url="http://test", home=home,
                          cwd=tmp_path, data_root=tmp_path,
                          client=_fake_backend(None))
    report = "\n".join(lines)
    assert "directory-scoped" in report
    assert "--scope user" in report


# -- desktop dispatch ----------------------------------------------------------


def test_doctor_flag_routes_to_doctor(monkeypatch):
    called = []
    monkeypatch.setattr(doctor, "main", lambda: called.append("doctor"))
    monkeypatch.setattr(
        desktop.uvicorn, "run", lambda *a, **k: called.append("uvicorn"))
    monkeypatch.setattr(desktop.sys, "argv", ["manifold-backend", "--doctor"])
    desktop.main()
    assert called == ["doctor"]
