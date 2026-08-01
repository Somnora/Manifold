import pytest
import json
from httpx import AsyncClient, ASGITransport

@pytest.fixture
async def async_api_client(client):
    async with AsyncClient(
        transport=ASGITransport(app=client.app), base_url="http://test"
    ) as async_client:
        yield async_client

@pytest.fixture
def mcp_client(async_api_client, monkeypatch):
    from app.mcp_server import _http
    monkeypatch.setattr("app.mcp_server._http", lambda: async_api_client)
    from app.mcp_server import mcp
    return mcp

@pytest.mark.asyncio
async def test_api_handshake_and_context(async_api_client):
    # Test AGY protocol
    resp = await async_api_client.post("/agent/handshake", json={"session_id": "test-session", "protocol": "AGY"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["context"]["session_id"] == "test-session"

    # Get context
    resp = await async_api_client.get("/agent/context/test-session")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "test-session"

    # Update context
    resp = await async_api_client.post("/agent/context/test-session/update", json={
        "workspace_environment": {"env": "prod"},
        "session_tokens": {"token": "123"}
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["context"]["workspace_environment"] == {"env": "prod"}
    assert data["context"]["session_tokens"] == {"token": "123"}

@pytest.mark.asyncio
async def test_mcp_handshake_and_context(mcp_client):
    # Test Claude/OpenClaw via MCP
    result = await mcp_client.call_tool("agent_handshake", {"session_id": "mcp-session", "protocol": "Claude", "note": "handshake"})
    data = json.loads(result[0].text)
    assert data["status"] == "ok"
    assert data["context"]["session_id"] == "mcp-session"

    # Get context
    result = await mcp_client.call_tool("get_agent_context", {"session_id": "mcp-session", "note": "get"})
    data = json.loads(result[0].text)
    assert data["session_id"] == "mcp-session"

    # Update context
    result = await mcp_client.call_tool("update_agent_context", {
        "session_id": "mcp-session",
        "active_gpu_connections": {"gpu": "A100"},
        "task_graphs": {"graph1": "running"},
        "note": "update"
    })
    data = json.loads(result[0].text)
    assert data["status"] == "ok"
    assert data["context"]["active_gpu_connections"] == {"gpu": "A100"}
    assert data["context"]["task_graphs"] == {"graph1": "running"}

@pytest.mark.asyncio
async def test_agy_integration():
    from app.agy_integration import AGYIntegration, AGYHandshakeRequest
    integration = AGYIntegration()
    req = AGYHandshakeRequest(agent_id="agy-agent-1", agent_version="1.0.0", requested_capabilities=["discover", "launch"])
    res = await integration.handshake(req)
    assert res.status == "authorized"
    assert "discover_instances" in res.authorized_capabilities

    substrate = await integration.discover_substrate()
    assert substrate["substrate_type"] == "manifold_compute"

    skill_yaml = integration.generate_skill_template()
    assert "manifold-orchestrator" in skill_yaml

@pytest.mark.asyncio
async def test_claude_integration(tmp_path):
    from app.claude_integration import init_claude_session
    session = await init_claude_session(str(tmp_path))
    assert session["session_id"].startswith("claude_session_")
    assert session["workspace"] == str(tmp_path)

@pytest.mark.asyncio
async def test_openclaw_adapter():
    from app.openclaw_adapter import openclaw_adapter, OpenClawHandshakeRequest, OpenClawToolSpec
    req = OpenClawHandshakeRequest(
        agent_id="openclaw-agent-1",
        framework="openclaw",
        tools=[OpenClawToolSpec(name="gpu_eval", description="Evaluates GPU workload")]
    )
    res = openclaw_adapter.handshake(req)
    assert res.agent_id == "openclaw-agent-1"
    assert res.status == "connected"
    assert "gpu_eval" in res.registered_tools

    agents = openclaw_adapter.list_agents()
    assert len(agents) >= 1
    assert openclaw_adapter.get_agent("openclaw-agent-1") is not None

