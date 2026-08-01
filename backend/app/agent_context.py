from dataclasses import dataclass, field
from typing import Dict, Any, Optional

@dataclass
class AgentContext:
    session_id: str
    workspace_environment: Dict[str, Any] = field(default_factory=dict)
    session_tokens: Dict[str, Any] = field(default_factory=dict)
    active_gpu_connections: Dict[str, Any] = field(default_factory=dict)
    task_graphs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_environment": self.workspace_environment,
            "session_tokens": self.session_tokens,
            "active_gpu_connections": self.active_gpu_connections,
            "task_graphs": self.task_graphs,
        }

    def update(self, updates: Dict[str, Any]) -> None:
        if "workspace_environment" in updates:
            self.workspace_environment.update(updates["workspace_environment"])
        if "session_tokens" in updates:
            self.session_tokens.update(updates["session_tokens"])
        if "active_gpu_connections" in updates:
            self.active_gpu_connections.update(updates["active_gpu_connections"])
        if "task_graphs" in updates:
            self.task_graphs.update(updates["task_graphs"])

class AgentContextManager:
    def __init__(self):
        self._contexts: Dict[str, AgentContext] = {}

    def create_context(self, session_id: str) -> AgentContext:
        context = AgentContext(session_id=session_id)
        self._contexts[session_id] = context
        return context

    def get_context(self, session_id: str) -> Optional[AgentContext]:
        return self._contexts.get(session_id)

    def update_context(self, session_id: str, updates: Dict[str, Any]) -> Optional[AgentContext]:
        context = self.get_context(session_id)
        if context:
            context.update(updates)
        return context

# Global context manager instance
agent_contexts = AgentContextManager()
