"""Google Antigravity (AGY) SDK Integration for Manifold.

This module provides the handshake protocol and skill configuration for
AGY agents to detect the Manifold compute substrate, negotiate authorization,
discover active GPU instances, and spawn remote tool execution.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel

class AGYHandshakeRequest(BaseModel):
    agent_id: str
    agent_version: str
    requested_capabilities: List[str]

class AGYHandshakeResponse(BaseModel):
    status: str
    substrate_id: str
    authorized_capabilities: List[str]
    endpoints: Dict[str, str]

class AGYIntegration:
    def __init__(self, orchestrator: Any = None):
        self.orchestrator = orchestrator

    async def handshake(self, req: AGYHandshakeRequest) -> AGYHandshakeResponse:
        """Negotiate authorization with an AGY agent."""
        return AGYHandshakeResponse(
            status="authorized",
            substrate_id="manifold-local",
            authorized_capabilities=["discover_instances", "launch_gpu", "run_job", "remote_tool_execution"],
            endpoints={
                "discover": "/instances",
                "launch": "/instances",
                "tasks": "/tasks",
                "clusters": "/clusters"
            }
        )

    async def discover_substrate(self) -> Dict[str, Any]:
        """Detect Manifold compute substrate."""
        # For simplicity, assuming list_instances and list_clusters exist on orchestrator
        # or we just return an empty skeleton if they are not exposed directly like this.
        return {
            "substrate_type": "manifold_compute",
            "message": "Substrate detected"
        }

    def generate_skill_template(self) -> str:
        """Create AGY skill configuration for AGY agent orchestration.

        LIMITATION (Phase 78): the AGY skill format carries a bare
        base_url and has no credential/header field, so an agent driving
        Manifold through only this template cannot authenticate. When the
        backend enforces MANIFOLD_API_TOKEN (real mode always does after
        first boot), such an agent gets 401s on every call. The note is
        embedded in the emitted YAML so the failure explains itself
        instead of breaking silently; AGY agents need mock mode
        (MANIFOLD_MOCK=1) or an explicitly emptied MANIFOLD_API_TOKEN.
        """
        return '''name: manifold-orchestrator
description: Manifold Compute Substrate for AGY
capabilities:
  - discover_instances
  - launch_gpu
  - run_job
  - launch_cluster
endpoints:
  base_url: "http://localhost:8000"
# NOTE: Manifold's API requires "Authorization: Bearer <MANIFOLD_API_TOKEN>"
# whenever a token is configured (real mode always is). This skill format
# has no credential field, so agents using only this base_url need the
# backend in mock mode (MANIFOLD_MOCK=1) or with MANIFOLD_API_TOKEN
# explicitly empty; expect 401s otherwise.
'''
