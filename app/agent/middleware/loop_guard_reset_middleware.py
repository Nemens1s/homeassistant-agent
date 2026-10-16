from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config

from app.tools.adapter import LoopGuard


def _current_thread_id() -> str:
    """The running graph's thread id; "default" matches the adapter's fallback."""
    try:
        config = get_config()
    except RuntimeError:  # called outside a graph run (tests)
        return "default"
    configurable = config.get("configurable") or {}
    return configurable.get("thread_id", "default")


class LoopGuardResetMiddleware(AgentMiddleware):
    """The guard dedupes within one run; a fresh run must start clean.
    Only this run's thread is reset — other threads may be mid-run."""

    def __init__(self, guard: LoopGuard):
        super().__init__()
        self._guard = guard

    def before_agent(self, state, runtime) -> dict | None:
        self._guard.reset(_current_thread_id())
        return None

    async def abefore_agent(self, state, runtime) -> dict | None:
        self._guard.reset(_current_thread_id())
        return None
