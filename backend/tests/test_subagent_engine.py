"""Local Subagent Engine: engine unit behaviour + the REAL backend routes.

The route tests hit the app wired by conftest's `client` fixture (mock mode),
not a hand-rolled simulated router - that simulated router was why the
broken /subagents/dispatch route (which raised on every call) still looked
green.

Instance-served endpoints ride the managed SSH forward (RealModelClient),
never a socket off the backend host - the forward path is unit-tested here
with a fake connection/listener. End-to-end dispatch to a REAL served model
can only be verified on GPU hardware at the phase gate (mock mode cannot
open a real SSH forward).
"""

import json
import types

import httpx
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from app.dispatcher import Dispatcher
from app.subagent_engine import (
    engine,
    NoHealthyEndpoint,
    SubagentDispatchError,
)


@pytest.fixture(autouse=True)
def reset_engine():
    engine.endpoints.clear()
    engine.adapters.clear()
    yield
    engine.endpoints.clear()
    engine.adapters.clear()


# -- fake managed connection (for the SSH-forward path) ----------------------


class _FakeListener:
    def __init__(self, local_port):
        self._local = local_port
        self.closed = False

    def get_port(self):
        return self._local

    def close(self):
        self.closed = True


class _FakeSSH:
    def __init__(self):
        self.forwards = []     # (local_host, local_port, remote_host, remote_port)
        self.listeners = []

    async def forward_local_port(self, lh, lp, rh, rp):
        self.forwards.append((lh, lp, rh, rp))
        listener = _FakeListener(50000 + len(self.listeners))
        self.listeners.append(listener)
        return listener


class _FakeManagedConnection:
    def __init__(self):
        self._ssh = _FakeSSH()
        self.host = "10.0.0.9"
        from app.connections import ConnectionState
        self.state = ConnectionState.CONNECTED

    def ssh_connection(self):
        return self._ssh


# -- engine unit tests -------------------------------------------------------


def test_register_endpoint():
    engine.register_endpoint("vllm-coder", url="http://localhost:8000")
    assert len(engine.endpoints["vllm-coder"]) == 1
    # Duplicate registration (same key) is a no-op.
    engine.register_endpoint("vllm-coder", url="http://localhost:8000")
    assert len(engine.endpoints["vllm-coder"]) == 1


def test_deregister_endpoint():
    engine.register_endpoint("m", url="http://a")
    engine.register_endpoint("m", url="http://b")
    engine.deregister_endpoint("m", key="http://a")
    assert [e.key for e in engine.endpoints["m"]] == ["http://b"]
    # The model key disappears once its last endpoint is gone.
    engine.deregister_endpoint("m", key="http://b")
    assert "m" not in engine.endpoints
    # Deregistering something never registered is a no-op, not an error.
    engine.deregister_endpoint("nope", key="http://x")


@pytest.mark.asyncio
async def test_dispatch_role_formatting():
    payload = engine.format_tool_call("Write python code", role="coding")
    assert "You are a specialized coding subagent." in \
        payload["messages"][0]["content"]
    assert payload["response_format"]["type"] == "json_object"

    with pytest.raises(ValueError, match="Invalid role"):
        engine.format_tool_call("Invalid", role="invalid_role")


def test_format_tool_call_passes_tools():
    payload = engine.format_tool_call(
        "prompt", tools=[{"name": "mcp-test", "description": "test tool"}])
    assert payload["tools"][0]["name"] == "mcp-test"


def test_multi_lora_adapter():
    engine.set_lora_adapter("vllm-coder", "lora-coder-v1")
    assert engine.adapters["vllm-coder"] == "lora-coder-v1"
    with pytest.raises(ValueError):
        engine.set_lora_adapter("vllm-coder", None)


@pytest.mark.asyncio
@patch("app.subagent_engine.httpx.AsyncClient")
async def test_dispatch_success_local(mock_client_class):
    """A local (backend-host) endpoint dispatches over plain loopback."""
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.get.return_value = MagicMock(status_code=200)   # health probe
    post_resp = MagicMock(status_code=200)
    post_resp.json.return_value = {"status": "success", "content": "test"}
    mock_client.post.return_value = post_resp

    engine.register_endpoint("vllm-reasoner", url="http://localhost:8000")
    res = await engine.dispatch("vllm-reasoner", {"messages": []})
    assert res["status"] == "success"


@pytest.mark.asyncio
async def test_dispatch_unreachable_endpoint():
    engine.register_endpoint("vllm-reasoner", url="http://localhost:8000")
    with patch.object(engine, "_check_endpoint", AsyncMock(return_value=False)):
        with pytest.raises(NoHealthyEndpoint,
                           match="No healthy endpoint for vllm-reasoner"):
            await engine.dispatch("vllm-reasoner", {"messages": []})


@pytest.mark.asyncio
@patch("app.subagent_engine.httpx.AsyncClient")
async def test_dispatch_invalid_json(mock_client_class):
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.get.return_value = MagicMock(status_code=200)   # health probe
    post_resp = MagicMock(status_code=200)
    post_resp.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
    mock_client.post.return_value = post_resp

    engine.register_endpoint("vllm-reasoner", url="http://localhost:8000")
    with pytest.raises(SubagentDispatchError,
                       match="Invalid JSON schema returned by endpoint"):
        await engine.dispatch("vllm-reasoner", {"messages": []})


@pytest.mark.asyncio
@patch("app.subagent_engine.httpx.AsyncClient")
async def test_dispatch_timeout(mock_client_class):
    """httpx raises httpx.TimeoutException (not asyncio.TimeoutError); the
    handler must catch the real exception and report it."""
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_client.get.return_value = MagicMock(status_code=200)   # health probe
    mock_client.post.side_effect = httpx.TimeoutException("timed out")

    engine.register_endpoint("vllm-reasoner", url="http://localhost:8000")
    with pytest.raises(SubagentDispatchError, match="timed out"):
        await engine.dispatch("vllm-reasoner", {"messages": []})


# -- instance-served endpoints ride the managed SSH forward ------------------


@pytest.mark.asyncio
@patch("app.model_client.httpx.AsyncClient")
async def test_dispatch_rides_managed_ssh_forward(mock_httpx):
    """An instance-served endpoint must reach the model through the managed
    connection's SSH local-port forward (RealModelClient), forwarding the
    instance's REAL remote port and closing the listener afterwards - never a
    raw socket off the backend host."""
    mock_http = AsyncMock()
    mock_httpx.return_value.__aenter__.return_value = mock_http
    # Health probe: GET /v1/models over the forward.
    get_resp = MagicMock(status_code=200)
    get_resp.json.return_value = {"object": "list", "data": [{"id": "served-model"}]}
    get_resp.raise_for_status = MagicMock()
    mock_http.get.return_value = get_resp
    # Dispatch: POST /v1/chat/completions over the forward.
    post_resp = MagicMock(status_code=200)
    post_resp.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
    mock_http.post.return_value = post_resp

    conn = _FakeManagedConnection()
    engine.register_endpoint("served-model", connection=conn,
                             remote_port=8000, key="inst-1:8000")

    payload = engine.format_tool_call("do it", role="coding")
    result = await engine.dispatch("served-model", payload)

    assert result["choices"][0]["message"]["content"] == "hi"
    # Every call rode the forward to the instance's real remote port (8000)...
    assert conn._ssh.forwards, "dispatch must forward through the managed connection"
    assert all(f == ("127.0.0.1", 0, "127.0.0.1", 8000)
               for f in conn._ssh.forwards)
    # ...and every ephemeral listener was closed afterwards (no leaks).
    assert conn._ssh.listeners
    assert all(listener.closed for listener in conn._ssh.listeners)
    # The served model name (not the "local" placeholder) went on the wire.
    assert mock_http.post.call_args.kwargs["json"]["model"] == "served-model"


@pytest.mark.asyncio
async def test_status_hides_connection_object(monkeypatch):
    """status() describes an instance-served endpoint without leaking the raw
    ManagedConnection object into a response."""
    engine.register_endpoint("served", connection=object(),
                             remote_port=8000, key="inst-1:8000")
    monkeypatch.setattr(engine, "_check_endpoint", AsyncMock(return_value=True))
    snap = await engine.status()
    desc = snap["models"][0]["active_endpoints"][0]
    assert "127.0.0.1:8000" in desc
    assert "object at 0x" not in desc      # no raw object repr


# -- REAL backend route tests (mock-mode app via conftest `client`) ----------


def test_dispatch_route_passes_tools_and_default_role(client, monkeypatch):
    """The old route called format_tool_call(prompt, tools) positionally, so
    `tools` landed in `role` and every call raised ValueError -> HTTP 500.
    With tools present the route must now succeed and route tools/role right."""
    captured = {}

    async def fake_dispatch(model, payload):
        captured["model"] = model
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(engine, "dispatch", fake_dispatch)

    res = client.post("/subagents/dispatch", json={
        "model": "vllm-coder",
        "prompt": "write a function",
        "tools": [{"type": "function", "function": {"name": "run"}}],
    })
    assert res.status_code == 200      # used to be 500
    assert res.json() == {"ok": True}
    assert captured["model"] == "vllm-coder"
    # tools reached the payload's tools slot, and the default role applied.
    assert captured["payload"]["tools"][0]["function"]["name"] == "run"
    assert "coding" in captured["payload"]["messages"][0]["content"]


def test_dispatch_route_honours_role(client, monkeypatch):
    captured = {}

    async def fake_dispatch(model, payload):
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(engine, "dispatch", fake_dispatch)

    res = client.post("/subagents/dispatch", json={
        "model": "vllm-reasoner", "prompt": "think", "role": "reasoning",
    })
    assert res.status_code == 200
    assert "reasoning" in captured["payload"]["messages"][0]["content"]


def test_dispatch_route_no_endpoint_returns_503(client):
    """No endpoint serving the model -> a clean 503, NOT a raw 500 with a
    stack trace. (No monkeypatch of engine.dispatch: this exercises the real
    NoHealthyEndpoint path end to end.)"""
    res = client.post("/subagents/dispatch",
                      json={"model": "not-serving", "prompt": "hi"})
    assert res.status_code == 503
    assert "No healthy endpoint" in res.json()["detail"]


def test_dispatch_route_upstream_error_returns_502(client, monkeypatch):
    """A reached-but-failing model surfaces as 502, not 500."""
    async def boom(model, payload):
        raise SubagentDispatchError("model returned 500: kaboom")

    monkeypatch.setattr(engine, "dispatch", boom)
    res = client.post("/subagents/dispatch",
                      json={"model": "m", "prompt": "hi"})
    assert res.status_code == 502
    assert "kaboom" in res.json()["detail"]


def test_dispatch_route_bad_role_returns_422(client):
    """A role format_tool_call rejects is a client error, not a 500."""
    res = client.post("/subagents/dispatch",
                      json={"model": "m", "prompt": "hi", "role": "nonsense"})
    assert res.status_code == 422


def test_models_route_reflects_registration(client, monkeypatch):
    engine.register_endpoint("vllm-coder", url="http://127.0.0.1:8000")
    monkeypatch.setattr(engine, "_check_endpoint", AsyncMock(return_value=True))

    res = client.get("/subagents/models")
    assert res.status_code == 200
    models = {m["model"]: m for m in res.json()["models"]}
    assert "vllm-coder" in models
    assert "http://127.0.0.1:8000" in models["vllm-coder"]["active_endpoints"]


def test_swarm_status_healthy_when_endpoint_registered(client, monkeypatch):
    engine.register_endpoint("vllm-coder", url="http://127.0.0.1:8000")
    monkeypatch.setattr(engine, "_check_endpoint", AsyncMock(return_value=True))

    res = client.get("/subagents/swarm/status")
    assert res.status_code == 200
    body = res.json()
    assert body["health"] == "ok"
    assert body["active_subagents"] == 1


# -- dispatcher registration wiring ------------------------------------------


def _dispatcher_with(templates):
    return Dispatcher(
        settings=MagicMock(), orchestrator=MagicMock(), queue=MagicMock(),
        templates=templates, db=MagicMock(), lambda_client=MagicMock(),
    )


def test_served_endpoint_maps_server_and_batch():
    server_tmpl = types.SimpleNamespace(ports=[types.SimpleNamespace(host=8000)])
    batch_tmpl = types.SimpleNamespace(ports=[])
    dispatcher = _dispatcher_with(
        {"vllm-serve": server_tmpl, "whisper-batch": batch_tmpl})

    # server task -> (model_id, remote_port int)
    assert dispatcher._served_endpoint(
        {"template": "vllm-serve", "parameters": {"model_id": "m"}}
    ) == ("m", 8000)
    # Falls back to the template name when no model_id is set.
    assert dispatcher._served_endpoint(
        {"template": "vllm-serve", "parameters": {}}
    ) == ("vllm-serve", 8000)
    # A batch template (no ports) exposes no subagent endpoint.
    assert dispatcher._served_endpoint(
        {"template": "whisper-batch", "parameters": {}}) is None


def test_finish_task_deregisters_served_model():
    """The completion funnel drops a settled server task's endpoint by its
    stable instance:port key - this is also the instance-torn-down path, since
    teardown settles the task here."""
    server_tmpl = types.SimpleNamespace(ports=[types.SimpleNamespace(host=8000)])
    dispatcher = _dispatcher_with({"vllm-serve": server_tmpl})
    dispatcher.queue.get.return_value = {
        "id": "task-1", "template": "vllm-serve", "instance_id": "inst-1",
        "parameters": {"model_id": "my-model"}, "status": "succeeded",
    }

    engine.register_endpoint("my-model", connection=object(),
                             remote_port=8000, key="inst-1:8000")
    assert "my-model" in engine.endpoints

    dispatcher._finish_task("task-1", exit_code=0, output_paths=[])
    assert "my-model" not in engine.endpoints
