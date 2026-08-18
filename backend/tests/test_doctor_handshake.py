"""Phase 106: the doctor performs the REAL MCP handshake.

Born from a real incident: Claude Desktop showed "MCP manifold: Couldn't
start this server ... Request timed out" while every other doctor check
reported all clear. The bridge was registered, the backend was up, and
the client still killed the spawn before `initialize` came back. These
tests pin the self-test that now answers that question directly - spawn
the bridge the way a client does, speak JSON-RPC to it, time it - without
needing the frozen app: the probe takes a spawn command, so most of it is
exercised against a tiny fake MCP server. One test at the end talks to
the real dev bridge.
"""

from __future__ import annotations

import json
import os
import sys
import time
import tomllib
from pathlib import Path

import httpx
import pytest

from app import doctor

# A minimal MCP server: enough protocol for one handshake and nothing
# else. Env vars switch on the misbehaviours we need to test, so the same
# script serves the happy path, the too-slow path and the malformed one.
FAKE_SERVER = '''
import json, os, subprocess, sys, time

delay = float(os.environ.get("FAKE_DELAY", "0"))
malformed = os.environ.get("FAKE_MALFORMED") == "1"
pid_file = os.environ.get("FAKE_PID_FILE")

if pid_file:
    # A grandchild in the same process group, standing in for the second
    # process a PyInstaller onefile bootloader execs.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    with open(pid_file, "w") as handle:
        handle.write(str(child.pid))

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        time.sleep(delay)
        if malformed:
            sys.stdout.write("this is not json\\n")
            sys.stdout.flush()
            continue
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fake-mcp", "version": "0"}}}
    elif method == "tools/list":
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": {
            "tools": [{"name": "one"}, {"name": "two"}]}}
    else:
        continue
    sys.stdout.write(json.dumps(reply) + "\\n")
    sys.stdout.flush()
'''


def fake_command() -> list[str]:
    return [sys.executable, "-c", FAKE_SERVER]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# -- the probe ---------------------------------------------------------------


def test_probe_completes_a_handshake_and_times_both_phases():
    result = doctor.probe_handshake(fake_command())
    assert result.ok, result.error
    assert result.error is None
    assert result.tool_count == 2          # from the reply, never assumed
    assert result.initialize_ms is not None and result.initialize_ms >= 0
    assert result.tools_ms is not None and result.tools_ms >= 0
    assert result.elapsed_ms >= result.initialize_ms


def test_probe_that_misses_the_deadline_fails_with_the_elapsed_time(monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "5")
    result = doctor.probe_handshake(fake_command(), deadline=0.5)
    assert not result.ok
    assert "initialize" in result.error
    assert result.elapsed_ms >= 500
    # Nothing was measured, so nothing is reported: no 0ms, no 0 tools.
    assert result.initialize_ms is None
    assert result.tools_ms is None
    assert result.tool_count is None


def test_probe_rejects_a_reply_that_is_not_json(monkeypatch):
    monkeypatch.setenv("FAKE_MALFORMED", "1")
    result = doctor.probe_handshake(fake_command(), deadline=5)
    assert not result.ok
    assert "not JSON" in result.error
    assert result.tool_count is None


def test_probe_reports_a_command_that_cannot_be_spawned(tmp_path):
    result = doctor.probe_handshake([str(tmp_path / "no-such-binary"), "--mcp"])
    assert not result.ok
    assert "could not spawn" in result.error


def test_probe_kills_the_whole_process_group(tmp_path, monkeypatch):
    """The onefile trap: killing only the process we spawned leaves its
    child alive holding the pipes."""
    pid_file = tmp_path / "grandchild.pid"
    monkeypatch.setenv("FAKE_PID_FILE", str(pid_file))
    result = doctor.probe_handshake(fake_command())
    assert result.ok, result.error

    grandchild = int(pid_file.read_text())
    give_up_at = time.monotonic() + 5
    while alive(grandchild) and time.monotonic() < give_up_at:
        time.sleep(0.05)
    assert not alive(grandchild), "the bridge's own child survived the probe"


def test_concurrent_probes_both_complete():
    results = doctor.probe_handshake_concurrent(fake_command(), count=2)
    assert len(results) == 2
    assert all(r.ok for r in results), [r.error for r in results]
    assert [r.tool_count for r in results] == [2, 2]


# -- the report rows ----------------------------------------------------------


def test_check_reports_timings_the_tool_count_and_the_concurrent_pair():
    lines, ok = doctor.check_handshake(command=fake_command())
    report = "\n".join(lines)
    assert ok, report
    assert "mcp handshake: initialize" in report
    assert "tools/list" in report
    assert "(2 tools)" in report
    assert "2 clients at once" in report
    assert "2 tools each" in report
    assert all(line.startswith(("  OK    ", "  FAIL  ", "  --    "))
               for line in lines)


def test_check_failure_carries_the_context_and_the_fix_commands(monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "5")
    lines, ok = doctor.check_handshake(command=fake_command(), deadline=0.5)
    report = "\n".join(lines)
    assert not ok
    assert "FAIL" in report
    assert "deadline 500ms" in report
    assert doctor.HANDSHAKE_FAILURE_CONTEXT in report
    assert "claude mcp add manifold" in report
    assert "docs/mcp-setup.md" in report


def test_handshake_report_has_a_header_and_a_verdict():
    lines, ok = doctor.handshake_report(command=fake_command())
    assert ok
    assert lines[0].startswith("manifold doctor: MCP handshake self-test")
    assert "clean:" in lines[-1]


# -- the spawn command --------------------------------------------------------


def test_dev_spawn_reproduces_the_manifold_mcp_entry_point():
    backend_dir = Path(doctor.__file__).resolve().parent.parent
    entry = tomllib.loads(
        (backend_dir / "pyproject.toml").read_text())["project"]["scripts"]
    assert entry["manifold-mcp"] == "app.mcp_server:main"

    command, cwd = doctor.bridge_spawn()
    assert command[0] == sys.executable
    assert command[1] == "-c"
    assert "from app import mcp_server" in command[2]
    assert "mcp_server.main()" in command[2]
    assert cwd is not None and Path(cwd) == backend_dir


def test_frozen_spawn_is_the_binary_a_client_is_configured_with(monkeypatch):
    monkeypatch.setattr(doctor.sys, "frozen", True, raising=False)
    command, cwd = doctor.bridge_spawn()
    assert command == [sys.executable, "--mcp"]
    assert cwd is None


# -- wiring into the doctor ---------------------------------------------------


def _fake_backend():
    """/health up, /instances open: enough for diagnose to get past the
    checks this file is not about."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "mock": True})
        return httpx.Response(200, json={"instances": [], "mock": True})
    return httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="http://test")


@pytest.fixture
def registered_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"manifold": {}}}))
    return home


def test_diagnose_appends_the_handshake_last(registered_home, tmp_path,
                                             monkeypatch):
    monkeypatch.delenv("MANIFOLD_API_TOKEN", raising=False)
    monkeypatch.setattr(doctor, "check_handshake",
                        lambda: (["  OK    mcp handshake: stubbed"], True))
    lines, ok = diagnose_with(registered_home, tmp_path, handshake=True)
    assert ok
    # Last check before the verdict line: it is the most expensive one.
    assert lines[-2] == "  OK    mcp handshake: stubbed"


def test_diagnose_handshake_failure_blocks(registered_home, tmp_path,
                                           monkeypatch):
    monkeypatch.delenv("MANIFOLD_API_TOKEN", raising=False)
    monkeypatch.setattr(doctor, "check_handshake",
                        lambda: (["  FAIL  mcp handshake: stubbed"], False))
    lines, ok = diagnose_with(registered_home, tmp_path, handshake=True)
    assert not ok
    assert "BLOCKED" in lines[-1]


def test_diagnose_does_not_spawn_anything_unless_asked(registered_home,
                                                       tmp_path, monkeypatch):
    monkeypatch.delenv("MANIFOLD_API_TOKEN", raising=False)

    def explode():
        raise AssertionError("the handshake ran without being asked for")
    monkeypatch.setattr(doctor, "check_handshake", explode)
    _lines, ok = diagnose_with(registered_home, tmp_path)
    assert ok


def diagnose_with(home, tmp_path, **kwargs):
    return doctor.diagnose(api_url="http://test", home=home, cwd=tmp_path,
                           data_root=tmp_path, client=_fake_backend(),
                           **kwargs)


def test_handshake_flag_runs_only_the_handshake(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(doctor, "check_handshake",
                        lambda **kw: (["  OK    stub row"], True))
    monkeypatch.setattr(doctor, "diagnose",
                        lambda **kw: ran.append(kw) or ([], True))
    monkeypatch.setattr(doctor.sys, "argv", ["manifold-doctor", "--handshake"])
    with pytest.raises(SystemExit) as exit_info:
        doctor.main()
    assert exit_info.value.code == 0
    assert ran == []
    assert "stub row" in capsys.readouterr().out


def test_handshake_failure_exits_nonzero(monkeypatch):
    monkeypatch.setattr(doctor, "check_handshake",
                        lambda **kw: (["  FAIL  stub row"], False))
    monkeypatch.setattr(doctor.sys, "argv", ["manifold-doctor", "--handshake"])
    with pytest.raises(SystemExit) as exit_info:
        doctor.main()
    assert exit_info.value.code == 1


@pytest.mark.parametrize("argv,expected", [
    (["manifold-doctor"], True),
    (["manifold-doctor", "--no-handshake"], False),
])
def test_full_run_includes_the_handshake_unless_opted_out(argv, expected,
                                                          monkeypatch):
    seen = {}

    def fake_diagnose(**kwargs):
        seen.update(kwargs)
        return ["stub"], True
    monkeypatch.setattr(doctor, "diagnose", fake_diagnose)
    monkeypatch.setattr(doctor.sys, "argv", argv)
    with pytest.raises(SystemExit):
        doctor.main()
    assert seen["handshake"] is expected


# -- the real bridge ----------------------------------------------------------


def test_dev_bridge_answers_a_real_handshake():
    """The one test that talks to the actual MCP bridge (~2s).

    Deliberately not `uv run manifold-mcp`: the entry point resolved
    under this interpreter is the same code, and a nested uv inside
    pytest is not reliable. It needs no backend - initialize and
    tools/list are answered by the bridge itself."""
    command, cwd = doctor.bridge_spawn()
    result = doctor.probe_handshake(command, cwd=cwd)
    assert result.ok, result.error
    assert result.tool_count is not None and result.tool_count > 0
    assert result.initialize_ms < doctor.HANDSHAKE_DEADLINE_SECONDS * 1000
