import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

# An agent context holds a live session's workspace/GPU/task-graph state so a
# reconnecting agent picks up where it left off. It is deliberately bounded
# two ways so it can never grow without limit: each context expires after
# this many seconds of inactivity, and at most this many are kept at once
# (the least-recently-seen is evicted to make room). Both are in-memory only.
DEFAULT_CONTEXT_TTL_SECONDS = 3600.0
DEFAULT_MAX_CONTEXTS = 128


@dataclass
class AgentContext:
    session_id: str
    workspace_environment: Dict[str, Any] = field(default_factory=dict)
    active_gpu_connections: Dict[str, Any] = field(default_factory=dict)
    task_graphs: Dict[str, Any] = field(default_factory=dict)
    # NOTE: there is intentionally no `session_tokens` field. Secrets live in
    # .env (see the project hard rules), never in an in-memory dict that any
    # caller who knows a session id could read back.

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_environment": self.workspace_environment,
            "active_gpu_connections": self.active_gpu_connections,
            "task_graphs": self.task_graphs,
        }

    def update(self, updates: Dict[str, Any]) -> None:
        # Only the three known, non-secret sections are mergeable; anything
        # else in `updates` (a stray session_tokens, say) is ignored.
        if "workspace_environment" in updates:
            self.workspace_environment.update(updates["workspace_environment"])
        if "active_gpu_connections" in updates:
            self.active_gpu_connections.update(updates["active_gpu_connections"])
        if "task_graphs" in updates:
            self.task_graphs.update(updates["task_graphs"])


class AgentContextManager:
    """In-memory registry of live agent-session contexts, bounded so it cannot
    grow without limit: a context expires after `ttl_seconds` of inactivity,
    and at most `max_contexts` are retained (least-recently-seen evicted to
    make room). Holds NO secrets - see AgentContext."""

    def __init__(self, ttl_seconds: float = DEFAULT_CONTEXT_TTL_SECONDS,
                 max_contexts: int = DEFAULT_MAX_CONTEXTS,
                 clock: Callable[[], float] = time.monotonic):
        self._contexts: Dict[str, AgentContext] = {}
        self._last_seen: Dict[str, float] = {}
        self._ttl = ttl_seconds
        self._max = max_contexts
        self._clock = clock

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [sid for sid, seen in self._last_seen.items()
                   if now - seen >= self._ttl]
        for sid in expired:
            self._contexts.pop(sid, None)
            self._last_seen.pop(sid, None)

    def _touch(self, session_id: str) -> None:
        self._last_seen[session_id] = self._clock()

    def _evict_if_full(self) -> None:
        # Make room for one new context by dropping the least-recently-seen.
        while len(self._contexts) >= self._max and self._last_seen:
            oldest = min(self._last_seen, key=self._last_seen.get)
            self._contexts.pop(oldest, None)
            self._last_seen.pop(oldest, None)

    def create_context(self, session_id: str) -> AgentContext:
        self._purge_expired()
        if session_id not in self._contexts:
            self._evict_if_full()
        context = AgentContext(session_id=session_id)
        self._contexts[session_id] = context
        self._touch(session_id)
        return context

    def get_context(self, session_id: str) -> Optional[AgentContext]:
        self._purge_expired()
        context = self._contexts.get(session_id)
        if context is not None:
            self._touch(session_id)
        return context

    def update_context(self, session_id: str,
                       updates: Dict[str, Any]) -> Optional[AgentContext]:
        self._purge_expired()
        context = self._contexts.get(session_id)
        if context:
            context.update(updates)
            self._touch(session_id)
        return context


# Global context manager instance
agent_contexts = AgentContextManager()
