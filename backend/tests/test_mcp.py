"""The MCP bridge: thin client, guard parity, safety hook, audit trail.

Tools are exercised against the real app wired through an in-process ASGI
transport — the same HTTP surface a live backend serves over the socket.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import app.mcp_server as mcp_server
from app.lambda_api import MockLambdaClient
from app.main import create_app
from tests.conftest import make_settings, mock_connect_fn


@pytest.fixture
def wired_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    """Real app + the MCP module's HTTP client pointed at it in-process."""
    from app.config import IdleSettings, TaskSettings, WatchSettings
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=60, poll_seconds=10),
        watches=WatchSettings(poll_seconds=60),
    )
    application = create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    return application


@pytest.fixture
async def mcp_wired(wired_app):
    """Run the app lifespan and point the MCP module at it, in-process."""
    from asgi_lifespan import LifespanManager
    async with LifespanManager(wired_app) as manager:
        old = mcp_server._client
        mcp_server._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://manifold.test",
        )
        yield wired_app
        await mcp_server._client.aclose()
        mcp_server._client = old


def test_mcp_server_is_structurally_thin():
    """The bridge may import httpx and the MCP SDK — never backend
    internals. No imports, no bypass: guards cannot be circumvented by a
    code path that does not exist."""
    import ast
    tree = ast.parse(Path(mcp_server.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(("." * node.level) + module)
    # asyncio is stdlib plumbing (retry sleep/deadline in wait_for_launch),
    # not a path into backend internals.
    allowed = {"__future__", "asyncio", "json", "os", "typing", "httpx",
               "mcp.server.fastmcp"}
    assert imported <= allowed, (
        f"MCP server imports beyond the thin-client allowlist: "
        f"{imported - allowed}"
    )


async def test_budget_guard_identical_for_mcp_and_dashboard(mcp_wired, mock_client):
    """THE gate test: an MCP launch is rejected by the budget guard with
    byte-identical detail to a dashboard launch."""
    # Dashboard path: direct HTTP POST, exactly what the launch form sends.
    resp = await mcp_server._http().post("/instances", json={
        "instance_type": "gpu_8x_a100_80gb_sxm4",   # $22.32/hr >> $4.00
        "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    assert resp.status_code == 409
    dashboard_detail = resp.json()["detail"]

    # MCP path: the tool an agent calls.
    result = await mcp_server.launch_gpu(
        instance_type="gpu_8x_a100_80gb_sxm4",
        region="us-east-1",
        filesystem="manifold-data",
        note="gate-6 parity test",
    )
    assert result["error"] == dashboard_detail
    assert "Budget guard" in result["error"]
    # Neither path reached the Lambda API.
    assert mock_client.launch_calls == []


async def test_region_guard_applies_to_mcp(mcp_wired, mock_client):
    result = await mcp_server.launch_gpu(
        instance_type="gpu_1x_a10", region="us-west-1",
        filesystem="manifold-data",
    )
    assert "Region mismatch" in result["error"]
    assert mock_client.launch_calls == []


async def test_terminate_blocked_returns_file_list(mcp_wired, mock_client):
    import asyncio
    launch = await mcp_server.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", note="for hook test",
    )
    launch_id = launch["launch"]["id"]
    for _ in range(200):
        status = await mcp_server.get_launch_status(launch_id)
        if status["status"] == "active":
            break
        await asyncio.sleep(0.02)
    instance_id = status["lambda_instance_id"]

    # Make the instance's files unsaveable, so the hook has something to
    # refuse (the default policy would rescue them and terminate cleanly).
    await mcp_server._http().put("/preferences", json={
        "data_safety": {"to_filesystem": False, "to_local": False}})

    # force=false: the hook returns evidence instead of terminating.
    result = await mcp_server.terminate_instance(instance_id)
    assert result["blocked"] is True
    paths = [f["path"] for f in result["unpersisted_files"]]
    assert "checkpoints/step-2000.safetensors" in paths
    assert mock_client.instances[instance_id].status == "active"   # still up

    # sync, then force is honest and final.
    sync = await mcp_server.sync_outputs(instance_id, note="save then stop")
    assert "ephemeral-backup" in sync["synced_to"]
    done = await mcp_server.terminate_instance(instance_id, force=True)
    assert done["terminated"] is True
    assert mock_client.instances[instance_id].status == "terminated"


async def test_wait_for_launch_blocks_until_ready(mcp_wired):
    """wait_for_launch parks server-side and returns the settled, enriched
    record in one call - no client poll loop."""
    launch = await mcp_server.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", note="wait tool test",
    )
    settled = await mcp_server.wait_for_launch(
        launch["launch"]["id"], timeout=5, note="await boot")
    assert settled["settled"] is True
    assert settled["phase"] == "ready"
    assert settled["status"] == "active"


class _FlakyClient:
    """Duck-types the two AsyncClient methods the bridge uses. The first
    `fail_first` request() calls raise ConnectError (a backend restart
    dropping the socket mid-park); the rest delegate to the real client."""

    def __init__(self, inner: httpx.AsyncClient, fail_first: int):
        self._inner = inner
        self._failures_left = fail_first
        self.transport_errors_raised = 0

    async def request(self, *args, **kwargs):
        if self._failures_left > 0:
            self._failures_left -= 1
            self.transport_errors_raised += 1
            raise httpx.ConnectError("connection dropped (backend restarting)")
        return await self._inner.request(*args, **kwargs)

    async def post(self, *args, **kwargs):   # _audit
        return await self._inner.post(*args, **kwargs)


async def test_wait_for_launch_absorbs_a_restart_mid_park(mcp_wired, monkeypatch):
    """A --reload restart drops the long-poll socket. The wait tool must
    reconnect and return the settled launch, not surface 'unreachable' for a
    launch that is actually fine."""
    launch = await mcp_server.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", note="restart-mid-wait test",
    )
    flaky = _FlakyClient(mcp_server._client, fail_first=1)
    monkeypatch.setattr(mcp_server, "_client", flaky)
    monkeypatch.setattr(mcp_server.asyncio, "sleep",
                        _instant_sleep)   # don't spend real retry seconds
    settled = await mcp_server.wait_for_launch(
        launch["launch"]["id"], timeout=10, note="await across restart")
    assert flaky.transport_errors_raised == 1        # the drop really happened
    assert settled["settled"] is True
    assert settled["status"] == "active"
    # The transport hiccup was absorbed, not surfaced. (The row's own `error`
    # column exists but holds no launch failure.)
    assert not settled.get("unreachable")
    assert not settled.get("error")


async def test_wait_for_launch_calm_when_backend_stays_down(mcp_wired, monkeypatch):
    """If the backend never answers within the window, the tool returns a
    structured 'restarting, call again' record - not a scary hard error.
    (Real retry sleep here - patching it to zero would hot-spin the loop.)"""
    flaky = _FlakyClient(mcp_server._client, fail_first=10_000)
    monkeypatch.setattr(mcp_server, "_client", flaky)
    result = await mcp_server.wait_for_launch(
        "some-launch", timeout=1, note="backend down")
    assert result["settled"] is False
    assert result["phase"] == "backend_restarting"
    assert "call wait_for_launch again" in result["phase_detail"].lower()


_real_sleep = asyncio.sleep


async def _instant_sleep(_seconds):
    # Zero-length but still a REAL yield to the event loop, so the coroutines
    # the test is waiting on (launch pipeline, server-side park) can run.
    await _real_sleep(0)


async def test_every_tool_call_is_audited(mcp_wired):
    await mcp_server.list_templates(note="looking for whisper")
    await mcp_server.launch_gpu(
        instance_type="gpu_8x_a100_80gb_sxm4", region="us-east-1",
        filesystem="manifold-data", note="too expensive on purpose",
    )
    resp = await mcp_server._http().get("/audit", params={"actor": "mcp"})
    entries = resp.json()["entries"]
    by_tool = {e["action"]: json.loads(e["detail"]) for e in entries}

    assert by_tool["list_templates"]["note"] == "looking for whisper"
    assert by_tool["list_templates"]["result"] == "ok"
    launch_entry = by_tool["launch_gpu"]
    assert launch_entry["note"] == "too expensive on purpose"
    assert launch_entry["result"].startswith("rejected: Budget guard")
    assert launch_entry["args"]["instance_type"] == "gpu_8x_a100_80gb_sxm4"


async def test_worked_example_transcribe_inbox_then_shut_down(mcp_wired, mock_client):
    """The docs' example conversation, tool by tool: transcribe everything
    in /inbox with whisper-large, then shut down."""
    import asyncio

    # Agent discovers what it can run and where files live.
    templates = await mcp_server.list_templates(note="find transcription")
    assert any(t["name"] == "whisper-batch" for t in templates["templates"])
    files = await mcp_server.list_persistent_files(prefix="")
    assert files["files"]                    # single filesystem auto-selected

    # Launch and wait until active.
    launch = await mcp_server.launch_gpu(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", note="GPU for whisper batch",
    )
    launch_id = launch["launch"]["id"]
    for _ in range(300):
        status = await mcp_server.get_launch_status(launch_id)
        if status["status"] in ("active", "failed"):
            break
        await asyncio.sleep(0.02)
    assert status["status"] == "active"
    instance_id = status["lambda_instance_id"]

    # Run whisper-large over /inbox; poll to completion; read logs.
    job = await mcp_server.run_job(
        "whisper-batch",
        {"input_dir": "inbox", "model_size": "large-v3"},
        note="transcribe inbox",
    )
    task_id = job["task"]["id"]
    for _ in range(300):
        job_status = await mcp_server.get_job_status(task_id)
        if job_status["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.02)
    assert job_status["status"] == "succeeded"
    assert "/lambda/nfs/manifold-data/transcripts" in job_status["output_paths"]
    # tail wide enough to reach the dispatch banner: the whisper template's
    # embedded transcriber script (~70 lines) is echoed back by the mock SSH,
    # so the early "docker run" line sits further back than 50 lines now.
    logs = await mcp_server.get_job_logs(task_id, tail=300)
    assert any("docker run" in l["line"] for l in logs["lines"])

    # Shut down. One call: terminate rescues the instance's ephemeral files to
    # the persistent volume (Phase 37) and then stops the billing. An agent no
    # longer has to know the hook->sync->force dance to leave a clean box.
    final = await mcp_server.terminate_instance(instance_id, note="job done")
    assert final["terminated"] is True
    assert "ephemeral-backup" in final["rescue"]["synced_to"]
    assert final["rescue"]["unsaved"] == []

    # The whole session is on the audit trail.
    resp = await mcp_server._http().get("/audit", params={"actor": "mcp"})
    tools_used = [e["action"] for e in resp.json()["entries"]]
    for expected in ("launch_gpu", "run_job", "get_job_logs",
                     "terminate_instance"):
        assert expected in tools_used


async def test_get_skill_returns_the_playbook(mcp_wired):
    """Agent onboarding: the skill doc is served through the same thin
    client, so any MCP-connected agent can learn the recipes first."""
    text = await mcp_server.get_skill(note="session start")
    assert "never around it" in text          # the one rule that matters
    assert "wait_for_launch" in text          # launch recipe
    assert "vllm-serve" in text               # serve recipe
    assert "terminate_instance" in text       # teardown recipe


async def test_unreachable_backend_is_not_reported_as_no_instances(
        mcp_wired, monkeypatch, tmp_path):
    """Auto-instance-selection over a dead backend used to answer
    "connected instances: (none)" - presenting a crashed backend as a
    healthy account with nothing running. It must say "unreachable"."""
    flaky = _FlakyClient(mcp_server._client, fail_first=10_000)
    monkeypatch.setattr(mcp_server, "_client", flaky)
    result = await mcp_server.download_file(
        "outputs/x.bin", str(tmp_path / "x.bin"), note="backend down")
    assert result.get("unreachable") is True
    assert "unreachable" in result["error"]
    assert "(none)" not in result["error"]


async def test_no_destructive_filesystem_tool_on_the_bridge():
    """The interlock from phase 62: a whole-volume destroy has no rescue
    path, so it stays a human action (type-the-name in the dashboard).
    The bridge may create filesystems but must never grow a tool that
    deletes one - this fails the build if anyone adds it."""
    tools = await mcp_server.mcp.list_tools()
    names = {t.name for t in tools}
    assert "create_filesystem" in names          # creation stays agent-safe
    assert "delete_filesystem" not in names
    assert not any("delete" in n and "filesystem" in n for n in names)


async def test_stream_job_logs_and_events(mcp_wired, client):
    """Test MCP 2.0 streaming tools and approval management endpoints."""
    # Test pending approvals list
    res = await mcp_server.get_pending_approvals()
    assert "approvals" in res
    assert isinstance(res["approvals"], list)

    # List tools check for new MCP tools
    tools = await mcp_server.mcp.list_tools()
    names = {t.name for t in tools}
    assert "stream_job_logs" in names
    assert "stream_task_events" in names
    assert "get_pending_approvals" in names
    assert "decide_approval" in names
