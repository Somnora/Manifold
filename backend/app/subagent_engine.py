import asyncio
import httpx
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger("manifold.subagent_engine")


class NoHealthyEndpoint(Exception):
    """No registered endpoint for the requested model is answering."""


class SubagentDispatchError(Exception):
    """A registered endpoint was reached but failed: a bad status, an invalid
    body, a transport error, or the managed SSH forward could not be set up."""


@dataclass
class SubagentEndpoint:
    """One place a subagent model can be reached, in one of two shapes.

    instance-served (the shape the dispatcher registers): `connection` is the
    instance's ManagedConnection and `remote_port` is the loopback port the
    served model listens on ON THE INSTANCE. Every call rides the managed SSH
    forward (via RealModelClient) - nothing dials the instance directly, per
    the project hard rule that only sshd listens off-loopback on a GPU box.

    local: `url` is a base URL for a brain on the BACKEND host itself
    (Ollama / LM Studio), which is correctly reached over plain loopback.

    `key` is the stable identity used for dedup and deregistration.
    """
    key: str
    connection: Any = None
    remote_port: Optional[int] = None
    url: Optional[str] = None

    @property
    def is_local(self) -> bool:
        return self.connection is None

    def describe(self) -> str:
        """A log/UI-safe label. Never exposes the raw connection object."""
        if self.is_local:
            return self.url or "(local)"
        return f"ssh-forward -> 127.0.0.1:{self.remote_port}"


class LocalSubagentEngine:
    """Model registry, health monitoring, and dispatch for local and
    instance-served subagents."""

    def __init__(self):
        # Registry of model name -> list of endpoints serving it.
        self.endpoints: Dict[str, List[SubagentEndpoint]] = {}
        self.queue = asyncio.Queue()
        self.adapters: Dict[str, str] = {}          # model -> lora_name
        self.loaded_adapters = set()                # (url, lora_name) pairs

    # -- registry ------------------------------------------------------------

    def register_endpoint(self, model: str, connection: Any = None,
                          remote_port: Optional[int] = None,
                          url: Optional[str] = None,
                          key: Optional[str] = None) -> None:
        """Advertise a model endpoint. Pass a ManagedConnection + remote_port
        for an instance-served model (reached over the SSH forward), or a url
        for a local brain. `key` is the stable identity used to deregister
        later (the dispatcher uses "<instance_id>:<port>")."""
        if key is None:
            key = url if url is not None else f"conn:{id(connection)}:{remote_port}"
        endpoints = self.endpoints.setdefault(model, [])
        if any(e.key == key for e in endpoints):
            return
        endpoints.append(SubagentEndpoint(
            key=key, connection=connection, remote_port=remote_port, url=url))
        logger.info(f"Registered subagent endpoint {model} ({key})")

    def deregister_endpoint(self, model: str, key: str) -> None:
        """Drop one endpoint by its stable key (its server task finished or its
        instance was torn down). The model key disappears with its last
        endpoint, so /subagents/models stops advertising a dead model."""
        endpoints = self.endpoints.get(model)
        if not endpoints:
            return
        remaining = [e for e in endpoints if e.key != key]
        if len(remaining) == len(endpoints):
            return
        if remaining:
            self.endpoints[model] = remaining
        else:
            self.endpoints.pop(model, None)
        logger.info(f"Deregistered subagent endpoint {model} ({key})")

    # -- health --------------------------------------------------------------

    async def _check_endpoint(self, endpoint: SubagentEndpoint) -> bool:
        """Is this endpoint answering GET /v1/models right now? Instance-served
        endpoints probe over the managed SSH forward (RealModelClient); local
        ones over plain loopback. Any failure reads as unhealthy."""
        try:
            if endpoint.is_local:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(
                        f"{endpoint.url.rstrip('/')}/v1/models")
                    return resp.status_code == 200
            # Instance-served: the ONLY path to the model is the managed
            # connection's SSH forward. RealModelClient owns that forward.
            from .model_client import RealModelClient
            await RealModelClient(endpoint.connection).model_info(
                endpoint.remote_port)
            return True
        except Exception:
            return False

    async def get_healthy_endpoint(
        self, model: str
    ) -> Optional[SubagentEndpoint]:
        for endpoint in self.endpoints.get(model, []):
            if await self._check_endpoint(endpoint):
                return endpoint
        return None

    async def status(self) -> Dict[str, Any]:
        """Health snapshot for the /subagents/* routes: per-model healthy
        endpoint descriptions and a total healthy count. Uses describe() only,
        so the raw connection objects never leak into a response."""
        models: List[dict] = []
        healthy_total = 0
        for model, endpoints in self.endpoints.items():
            healthy = []
            for endpoint in endpoints:
                if await self._check_endpoint(endpoint):
                    healthy.append(endpoint.describe())
                    healthy_total += 1
            models.append({"model": model, "active_endpoints": healthy})
        return {"models": models, "healthy": healthy_total}

    # -- adapters ------------------------------------------------------------

    async def ensure_adapter_loaded(self, url: str, lora_name: str):
        cache_key = (url, lora_name)
        if cache_key in self.loaded_adapters:
            return
        # Manage dynamic adapter loading on vLLM / SGLang (local url path).
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{url.rstrip('/')}/v1/lora/adapters/load",
                    json={"lora_name": lora_name, "lora_path": lora_name}
                )
                if resp.status_code < 400:
                    self.loaded_adapters.add(cache_key)
                    logger.info(f"Dynamically loaded adapter {lora_name} at {url}")
                else:
                    logger.warning(f"Failed to dynamically load {lora_name}: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Dynamic adapter loading unsupported or errored: {e}")

    def set_lora_adapter(self, model: str, adapter_name: str):
        """Multi-LoRA adapter selection."""
        if not adapter_name or not isinstance(adapter_name, str):
            raise ValueError("Invalid LoRA adapter parameter.")
        self.adapters[model] = adapter_name

    # -- request formatting --------------------------------------------------

    def format_tool_call(self, prompt: str, role: str = "coding",
                         tools: List[Dict[str, Any]] = None) -> dict:
        """Structured JSON output & tool calling formatting for local vLLM/SGLang endpoints"""
        if role not in ["coding", "reasoning", "orchestration"]:
            raise ValueError(f"Invalid role: {role}")

        system_prompt = f"You are a specialized {role} subagent."
        payload = {
            "model": "local",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        if tools:
            payload["tools"] = tools
        return payload

    # -- dispatch ------------------------------------------------------------

    def _effective_payload(self, model: str, payload: dict,
                           lora_name: Optional[str]) -> tuple[dict, Optional[str]]:
        """Copy the payload with the real served-model name (or LoRA adapter)
        in its `model` field, and report which adapter (if any) is in play.
        format_tool_call leaves a placeholder "local" there."""
        adapter = lora_name or self.adapters.get(model)
        return {**payload, "model": adapter or model}, adapter

    async def dispatch(self, model: str, payload: dict,
                       lora_name: Optional[str] = None) -> dict:
        endpoint = await self.get_healthy_endpoint(model)
        if endpoint is None:
            raise NoHealthyEndpoint(f"No healthy endpoint for {model}")
        body, adapter = self._effective_payload(model, payload, lora_name)
        if endpoint.is_local:
            if adapter:
                await self.ensure_adapter_loaded(endpoint.url, adapter)
            return await self._dispatch_local(endpoint, body)
        return await self._dispatch_over_connection(endpoint, body)

    async def _dispatch_local(self, endpoint: SubagentEndpoint,
                              body: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{endpoint.url.rstrip('/')}/v1/chat/completions", json=body)
                if resp.status_code >= 400:
                    raise SubagentDispatchError(
                        f"Subagent request failed with {resp.status_code}: {resp.text}")
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    raise SubagentDispatchError(
                        "Invalid JSON schema returned by endpoint.")
        except httpx.TimeoutException:
            # httpx raises its own TimeoutException, not asyncio.TimeoutError.
            raise SubagentDispatchError(
                f"Subagent request to {endpoint.url} timed out.")

    async def _dispatch_over_connection(self, endpoint: SubagentEndpoint,
                                        body: dict) -> dict:
        # RealModelClient forwards the instance's loopback port to an ephemeral
        # local port, POSTs over it, and closes the listener - the same pattern
        # the chat panel uses. Nothing new listens off-loopback on the box.
        from .model_client import ModelClientError, RealModelClient
        try:
            return await RealModelClient(endpoint.connection).chat_completion(
                endpoint.remote_port, body)
        except ModelClientError as exc:
            raise SubagentDispatchError(str(exc)) from exc

    async def dispatch_stream(self, model: str, payload: dict,
                              lora_name: Optional[str] = None) -> AsyncIterator[str]:
        endpoint = await self.get_healthy_endpoint(model)
        if endpoint is None:
            raise NoHealthyEndpoint(f"No healthy endpoint for {model}")
        body, adapter = self._effective_payload(model, payload, lora_name)
        if endpoint.is_local:
            if adapter:
                await self.ensure_adapter_loaded(endpoint.url, adapter)
            async for line in self._stream_local(endpoint, body):
                yield line
            return
        from .model_client import ModelClientError, RealModelClient
        try:
            async for line in RealModelClient(endpoint.connection).chat_stream(
                    endpoint.remote_port, body):
                yield line
        except ModelClientError as exc:
            raise SubagentDispatchError(str(exc)) from exc

    async def _stream_local(self, endpoint: SubagentEndpoint,
                            body: dict) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST", f"{endpoint.url.rstrip('/')}/v1/chat/completions",
                json=body,
            ) as resp:
                if resp.status_code >= 400:
                    raw = (await resp.aread()).decode(errors="replace")
                    raise SubagentDispatchError(
                        f"Subagent stream failed with {resp.status_code}: {raw}")
                async for line in resp.aiter_lines():
                    if line:
                        yield line + "\n"


# Global instance for shared use
engine = LocalSubagentEngine()
