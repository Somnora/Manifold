import asyncio
import httpx
import json
import logging
from typing import AsyncIterator, Dict, List, Optional, Any

logger = logging.getLogger("manifold.subagent_engine")

class LocalSubagentEngine:
    """Model Registry & Health Monitoring and Subagent Task Dispatcher"""
    def __init__(self):
        # Registry of model names to list of endpoint base URLs
        self.endpoints: Dict[str, List[str]] = {}
        self.queue = asyncio.Queue()
        self.adapters: Dict[str, str] = {} # model -> lora_name
        self.loaded_adapters = set() # (url, lora_name) pairs

    async def ensure_adapter_loaded(self, url: str, lora_name: str):
        cache_key = (url, lora_name)
        if cache_key in self.loaded_adapters:
            return
        
        # Manage dynamic adapter loading on vLLM / SGLang
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # vLLM / SGLang dynamic LoRA loading endpoint
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

    def register_endpoint(self, model: str, url: str):
        if model not in self.endpoints:
            self.endpoints[model] = []
        if url not in self.endpoints[model]:
            self.endpoints[model].append(url)
            logger.info(f"Registered {model} at {url}")

    async def check_health(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{url.rstrip('/')}/v1/models")
                return resp.status_code == 200
        except Exception:
            return False

    async def get_healthy_endpoint(self, model: str) -> Optional[str]:
        endpoints = self.endpoints.get(model, [])
        for url in endpoints:
            if await self.check_health(url):
                return url
        return None

    def set_lora_adapter(self, model: str, adapter_name: str):
        """Multi-LoRA adapter selection."""
        if not adapter_name or not isinstance(adapter_name, str):
            raise ValueError("Invalid LoRA adapter parameter.")
        self.adapters[model] = adapter_name

    def format_tool_call(self, prompt: str, role: str = "coding", tools: List[Dict[str, Any]] = None) -> dict:
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

    async def dispatch(self, model: str, payload: dict, lora_name: Optional[str] = None) -> dict:
        url = await self.get_healthy_endpoint(model)
        if not url:
            raise Exception(f"No healthy endpoint for {model}")
            
        adapter = lora_name or self.adapters.get(model)
        if adapter:
            payload["model"] = adapter
            await self.ensure_adapter_loaded(url, adapter)
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{url.rstrip('/')}/v1/chat/completions", json=payload)
                if resp.status_code >= 400:
                    raise Exception(f"Subagent request failed with {resp.status_code}: {resp.text}")
                
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    raise Exception("Invalid JSON schema returned by endpoint.")
                return data
        except asyncio.TimeoutError:
            raise Exception("Queue timeout while dispatching task.")

    async def dispatch_stream(self, model: str, payload: dict, lora_name: Optional[str] = None) -> AsyncIterator[str]:
        url = await self.get_healthy_endpoint(model)
        if not url:
            raise Exception(f"No healthy endpoint for {model}")
            
        adapter = lora_name or self.adapters.get(model)
        if adapter:
            payload["model"] = adapter
            await self.ensure_adapter_loaded(url, adapter)
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", f"{url.rstrip('/')}/v1/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise Exception(f"Subagent stream failed with {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if line:
                        yield line + "\n"

# Global instance for shared use
engine = LocalSubagentEngine()
