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

    # Update context. A stray session_tokens is ignored, never stored: the
    # context schema has no such field (secrets belong in .env).
    resp = await async_api_client.post("/agent/context/test-session/update", json={
        "workspace_environment": {"env": "prod"},
        "session_tokens": {"token": "123"}
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["context"]["workspace_environment"] == {"env": "prod"}
    assert "session_tokens" not in data["context"]

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

def test_agent_context_has_no_session_tokens():
    """The schema must not carry a secrets bucket, and update() must ignore
    one if a caller sends it anyway."""
    from app.agent_context import AgentContext
    ctx = AgentContext(session_id="s1")
    assert not hasattr(ctx, "session_tokens")
    assert "session_tokens" not in ctx.to_dict()
    ctx.update({"session_tokens": {"token": "abc"},
                "workspace_environment": {"a": 1}})
    assert "session_tokens" not in ctx.to_dict()
    assert ctx.to_dict()["workspace_environment"] == {"a": 1}


def test_agent_context_ttl_expiry():
    """A context expires after ttl_seconds of inactivity; access refreshes it."""
    from app.agent_context import AgentContextManager
    now = {"t": 0.0}
    mgr = AgentContextManager(ttl_seconds=100.0, max_contexts=10,
                              clock=lambda: now["t"])
    mgr.create_context("s1")
    now["t"] = 50.0
    assert mgr.get_context("s1") is not None      # access refreshes last-seen
    now["t"] = 120.0                              # only 70s since that access
    assert mgr.get_context("s1") is not None
    now["t"] = 500.0                              # long past the TTL now
    assert mgr.get_context("s1") is None


def test_agent_context_cap_evicts_oldest():
    """At most max_contexts are kept; the least-recently-seen is evicted."""
    from app.agent_context import AgentContextManager
    now = {"t": 0.0}
    mgr = AgentContextManager(ttl_seconds=1e9, max_contexts=2,
                              clock=lambda: now["t"])
    mgr.create_context("s1"); now["t"] += 1
    mgr.create_context("s2"); now["t"] += 1
    mgr.create_context("s3")                      # over cap -> evict s1
    assert mgr.get_context("s1") is None
    assert mgr.get_context("s2") is not None
    assert mgr.get_context("s3") is not None
