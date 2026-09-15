from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from app.tools.adapter import LoopGuard


class LoopGuardResetMiddleware(AgentMiddleware):
    """The guard dedupes within one run; a fresh run must start clean."""

    def __init__(self, guard: LoopGuard):
        super().__init__()
        self._guard = guard

    def before_agent(self, state, runtime) -> dict | None:
        self._guard.reset()
        return None

    async def abefore_agent(self, state, runtime) -> dict | None:
        self._guard.reset()
        return None
