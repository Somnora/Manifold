"""ModelClient: talk to a model served ON an instance (e.g. by vllm-serve).

Same seam pattern as SidecarClient: an interface with a real implementation
that rides an SSH local port forward on the managed connection, and a mock
for tests/mock mode. The served model's API (vLLM's OpenAI-compatible
server) listens on 127.0.0.1 on the instance — the dispatcher publishes
template ports loopback-only — so the ONLY path to it is the managed
connection. Nothing new listens anywhere.

    browser chat panel
        -> backend POST /instances/{id}/chat   (SSE relay)
        -> SSH local port forward (managed connection)
        -> 127.0.0.1:<port> on the instance    (vLLM /v1/chat/completions)
"""

from __future__ import annotations

import abc
import asyncio
import json
from typing import AsyncIterator

import httpx

from .subagent_engine import engine as subagent_engine

class ModelClientError(Exception):
    pass


class ModelClient(abc.ABC):
    @abc.abstractmethod
    async def model_info(self, port: int) -> dict:
        """GET /v1/models on the served endpoint (also a liveness probe)."""

    @abc.abstractmethod
    def chat_stream(self, port: int, payload: dict) -> AsyncIterator[str]:
        """POST /v1/chat/completions with stream=true; yields raw SSE lines
        (each already newline-terminated) exactly as the server sent them."""

    @abc.abstractmethod
    async def chat_completion(self, port: int, payload: dict) -> dict:
        """POST /v1/chat/completions with stream=false; returns the full
        OpenAI response object (choices + usage) verbatim."""


class RealModelClient(ModelClient):
    """Reaches the served model through an SSH local port forward.

    The forward is created per call and closed after, mirroring
    RealSidecarClient — the managed connection itself stays up.
    """

    def __init__(self, managed_connection):
        self._mc = managed_connection

    async def _forward(self, remote_port: int):
        conn = self._mc.ssh_connection()
        if conn is None:
            raise ModelClientError(
                f"no SSH connection to {self._mc.host} "
                f"(state: {self._mc.state.value})"
            )
        try:
            return await conn.forward_local_port(
                "127.0.0.1", 0, "127.0.0.1", remote_port
            )
        except Exception as exc:
            raise ModelClientError(
                f"could not forward model port {remote_port}: {exc}"
            ) from exc

    async def model_info(self, port: int) -> dict:
        listener = await self._forward(port)
        try:
            local = listener.get_port()
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(f"http://127.0.0.1:{local}/v1/models")
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            raise ModelClientError(f"model endpoint not responding: {exc}") from exc
        finally:
            listener.close()

    async def chat_stream(self, port: int, payload: dict) -> AsyncIterator[str]:
        listener = await self._forward(port)
        try:
            local = listener.get_port()
            # No read timeout: token generation can pause arbitrarily long.
            timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as http:
                async with http.stream(
                    "POST",
                    f"http://127.0.0.1:{local}/v1/chat/completions",
                    json={**payload, "stream": True},
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode(errors="replace")
                        raise ModelClientError(
                            f"model returned {resp.status_code}: {body[:300]}"
                        )
                    async for line in resp.aiter_lines():
                        yield line + "\n"
        except httpx.HTTPError as exc:
            raise ModelClientError(f"chat stream failed: {exc}") from exc
        finally:
            listener.close()

    async def chat_completion(self, port: int, payload: dict) -> dict:
        listener = await self._forward(port)
        try:
            local = listener.get_port()
            timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as http:
                resp = await http.post(
                    f"http://127.0.0.1:{local}/v1/chat/completions",
                    json={**payload, "stream": False},
                )
                if resp.status_code >= 400:
                    raise ModelClientError(
                        f"model returned {resp.status_code}: {resp.text[:300]}"
                    )
                return resp.json()
        except httpx.HTTPError as exc:
            raise ModelClientError(f"chat completion failed: {exc}") from exc
        finally:
            listener.close()


class MockModelClient(ModelClient):
    """Canned OpenAI-compatible endpoint for tests and mock mode.

    When the conversation is an Autopilot run (recognizable by its system
    prompt), it plays a small scripted agent — inspect the catalog, check
    instances, finish — so the whole loop is demoable in mock mode with
    zero spend. Plain chats get the echo reply."""

    AUTOPILOT_SCRIPT = [
        '{"thought": "First see what GPUs exist and what they cost.",'
        ' "action": "list_instance_types", "args": {}}',
        '{"thought": "Now check what is currently running.",'
        ' "action": "list_instances", "args": {}}',
        '{"thought": "I have surveyed the account; reporting back.",'
        ' "action": "done", "args": {"summary": "Demo run complete: '
        'inspected the GPU catalog and the running instances. In a real '
        'run I would launch, run jobs, and terminate based on your goal."}}',
    ]

    def __init__(self, model_id: str = "mock/llama-3-8b"):
        self.model_id = model_id
        self.requests: list[dict] = []

    async def model_info(self, port: int) -> dict:
        return {"object": "list", "data": [{"id": self.model_id, "object": "model"}]}

    async def chat_stream(self, port: int, payload: dict) -> AsyncIterator[str]:
        self.requests.append({"port": port, "payload": payload})
        messages = payload.get("messages", [])
        system = messages[0]["content"] if messages else ""
        if system.startswith("You are Manifold Autopilot"):
            turn = sum(1 for m in messages if m["role"] == "assistant")
            idx = min(turn, len(self.AUTOPILOT_SCRIPT) - 1)
            words = self.AUTOPILOT_SCRIPT[idx].split(" ")
        else:
            last = messages[-1]["content"] if messages else ""
            words = f"Mock reply to: {last}".split(" ")
        for i, word in enumerate(words):
            chunk = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "model": self.model_id,
                "choices": [{
                    "index": 0,
                    "delta": {"content": ("" if i == 0 else " ") + word},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            await asyncio.sleep(0)   # yield control, like a real stream
        yield "data: [DONE]\n\n"

    async def chat_completion(self, port: int, payload: dict) -> dict:
        self.requests.append({"port": port, "payload": payload})
        messages = payload.get("messages", [])
        last = messages[-1]["content"] if messages else ""
        content = f"Mock reply to: {last}"
        tokens = len(content.split())
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": payload.get("model") or self.model_id,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": tokens,
                      "total_tokens": tokens},
        }


class SubagentModelClient(ModelClient):
    """Reaches a local subagent model via the LocalSubagentEngine.
    """

    def __init__(self, model_id: str):
        self.model_id = model_id

    async def model_info(self, port: int) -> dict:
        url = await subagent_engine.get_healthy_endpoint(self.model_id)
        if not url:
            raise ModelClientError(f"No healthy subagent endpoint for {self.model_id}")
        return {"object": "list", "data": [{"id": self.model_id, "object": "model"}]}

    async def chat_stream(self, port: int, payload: dict) -> AsyncIterator[str]:
        try:
            async for line in subagent_engine.dispatch_stream(self.model_id, payload):
                yield line
        except Exception as exc:
            raise ModelClientError(f"subagent chat stream failed: {exc}") from exc

    async def chat_completion(self, port: int, payload: dict) -> dict:
        try:
            return await subagent_engine.dispatch(self.model_id, payload)
        except Exception as exc:
            raise ModelClientError(f"subagent chat completion failed: {exc}") from exc
