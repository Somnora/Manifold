"""OpenClaw & Hermes Agent Protocol Adapter for Manifold.

Enables OpenClaw and Hermes agents to seamlessly register tools, synchronize
state, and execute tasks on Manifold instances and GPU clusters.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class OpenClawToolSpec(BaseModel):
    name: str
    description: str
    parameters: Dict[str, Any] = Field(default_factory=dict)


class OpenClawHandshakeRequest(BaseModel):
    agent_id: str
    framework: str = "openclaw"  # "openclaw" | "hermes"
    version: str = "1.0.0"
    tools: List[OpenClawToolSpec] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class OpenClawHandshakeResponse(BaseModel):
    agent_id: str
    status: str = "connected"
    manifold_substrate_id: str = "manifold-gpu-os"
    registered_tools: List[str]
    capabilities: List[str]


class OpenClawAdapter:
    """Manages OpenClaw and Hermes agent registrations and tool adapters."""

    def __init__(self) -> None:
        self.connected_agents: Dict[str, Dict[str, Any]] = {}
        self.registered_tools: Dict[str, OpenClawToolSpec] = {}

    def handshake(self, request: OpenClawHandshakeRequest) -> OpenClawHandshakeResponse:
        self.connected_agents[request.agent_id] = {
            "framework": request.framework,
            "version": request.version,
            "metadata": request.metadata,
        }
        
        tool_names = []
        for tool in request.tools:
            self.registered_tools[f"{request.agent_id}:{tool.name}"] = tool
            tool_names.append(tool.name)

        capabilities = [
            "manifold_instances",
            "gpu_clusters",
            "mcp_streaming",
            "container_dispatch",
            "persistent_storage",
        ]

        return OpenClawHandshakeResponse(
            agent_id=request.agent_id,
            status="connected",
            registered_tools=tool_names,
            capabilities=capabilities,
        )

    def list_agents(self) -> List[Dict[str, Any]]:
        return [
            {"agent_id": agent_id, **data}
            for agent_id, data in self.connected_agents.items()
        ]

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self.connected_agents.get(agent_id)


# Global singleton
openclaw_adapter = OpenClawAdapter()
