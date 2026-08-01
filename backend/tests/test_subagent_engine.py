import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from httpx import AsyncClient
from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient

from app.subagent_engine import engine, LocalSubagentEngine

# Simulated router since it's not yet integrated into main.py
subagent_router = APIRouter(prefix="/subagents")

@subagent_router.post("/dispatch")
async def dispatch_task(payload: dict):
    model = payload.get("model", "default-model")
    return await engine.dispatch(model, payload)

@subagent_router.get("/models")
async def list_models():
    return {"models": list(engine.endpoints.keys())}

@subagent_router.get("/swarm/status")
async def swarm_status():
    return {"healthy_endpoints": {k: len(v) for k, v in engine.endpoints.items()}}

app = FastAPI()
app.include_router(subagent_router)

client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_engine():
    engine.endpoints.clear()
    engine.adapters.clear()
    yield

def test_register_endpoint():
    engine.register_endpoint("vllm-coder", "http://localhost:8000")
    assert "http://localhost:8000" in engine.endpoints["vllm-coder"]
    # Test duplicate registration
    engine.register_endpoint("vllm-coder", "http://localhost:8000")
    assert len(engine.endpoints["vllm-coder"]) == 1

@pytest.mark.asyncio
async def test_dispatch_role_formatting():
    payload = engine.format_tool_call("Write python code", role="coding")
    assert "You are a specialized coding subagent." in payload["messages"][0]["content"]
    assert payload["response_format"]["type"] == "json_object"
    
    with pytest.raises(ValueError, match="Invalid role"):
        engine.format_tool_call("Invalid", role="invalid_role")

def test_multi_lora_adapter():
    engine.set_lora_adapter("vllm-coder", "lora-coder-v1")
    assert engine.adapters["vllm-coder"] == "lora-coder-v1"
    with pytest.raises(ValueError):
        engine.set_lora_adapter("vllm-coder", None)

@pytest.mark.asyncio
@patch("app.subagent_engine.httpx.AsyncClient")
async def test_dispatch_success(mock_client_class):
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    
    # Mock health check and post
    mock_client.get.return_value = MagicMock(status_code=200)
    
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"status": "success", "content": "test"}
    mock_client.post.return_value = mock_response

    engine.register_endpoint("vllm-reasoner", "http://localhost:8000")
    
    # Using patch directly on check_health to speed it up
    with patch.object(engine, "check_health", return_value=True):
        res = await engine.dispatch("vllm-reasoner", {"prompt": "test"})
    assert res["status"] == "success"

@pytest.mark.asyncio
async def test_dispatch_unreachable_endpoint():
    engine.register_endpoint("vllm-reasoner", "http://localhost:8000")
    with patch.object(engine, "check_health", return_value=False):
        with pytest.raises(Exception, match="No healthy endpoint for vllm-reasoner"):
            await engine.dispatch("vllm-reasoner", {"prompt": "test"})

@pytest.mark.asyncio
@patch("app.subagent_engine.httpx.AsyncClient")
async def test_dispatch_invalid_json(mock_client_class):
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    
    mock_response = MagicMock(status_code=200)
    # Simulate invalid JSON by raising json decode error
    import json
    mock_response.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
    mock_client.post.return_value = mock_response

    engine.register_endpoint("vllm-reasoner", "http://localhost:8000")
    with patch.object(engine, "check_health", return_value=True):
        with pytest.raises(Exception, match="Invalid JSON schema returned by endpoint"):
            await engine.dispatch("vllm-reasoner", {"prompt": "test"})

@pytest.mark.asyncio
@patch("app.subagent_engine.httpx.AsyncClient")
async def test_dispatch_queue_timeout(mock_client_class):
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.post.side_effect = asyncio.TimeoutError()

    engine.register_endpoint("vllm-reasoner", "http://localhost:8000")
    with patch.object(engine, "check_health", return_value=True):
        with pytest.raises(Exception, match="Queue timeout while dispatching task"):
            await engine.dispatch("vllm-reasoner", {"prompt": "test"})

# API Endpoint Tests
def test_api_models():
    engine.register_endpoint("vllm-coder", "http://localhost:8000")
    res = client.get("/subagents/models")
    assert res.status_code == 200
    assert "vllm-coder" in res.json()["models"]

def test_api_swarm_status():
    engine.register_endpoint("vllm-coder", "http://localhost:8000")
    engine.register_endpoint("vllm-coder", "http://localhost:8001")
    res = client.get("/subagents/swarm/status")
    assert res.status_code == 200
    assert res.json()["healthy_endpoints"]["vllm-coder"] == 2

@patch("app.subagent_engine.LocalSubagentEngine.dispatch")
def test_api_dispatch(mock_dispatch):
    # Need to return an async function
    async def mock_coro(*args, **kwargs):
        return {"result": "dispatched"}
    mock_dispatch.side_effect = mock_coro
    res = client.post("/subagents/dispatch", json={"model": "vllm-coder"})
    assert res.status_code == 200
    assert res.json() == {"result": "dispatched"}

# MCP Tools integration test
def test_mcp_tools_integration():
    payload = engine.format_tool_call("prompt", tools=[{"name": "mcp-test", "description": "test tool"}])
    assert "tools" in payload
    assert payload["tools"][0]["name"] == "mcp-test"
