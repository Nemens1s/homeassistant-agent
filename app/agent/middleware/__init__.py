from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from app.agent.middleware.context_window_middleware import ContextWindowMiddleware
from app.agent.middleware.history_cap_middleware import HistoryCapMiddleware
from app.agent.middleware.loop_guard_reset_middleware import LoopGuardResetMiddleware
from app.agent.middleware.tool_subset_middleware import ToolSubsetMiddleware
from app.config import Settings
from app.tools.adapter import LoopGuard


def build_middleware(settings: Settings, guard: LoopGuard, base_prompt: str, budget: int) -> list[AgentMiddleware]:
    middleware: list[AgentMiddleware] = [
        ContextWindowMiddleware(base_prompt, budget),
        LoopGuardResetMiddleware(guard),
    ]
    if settings.max_history_messages > 0:
        middleware.append(HistoryCapMiddleware(settings.max_history_messages))
    if settings.enable_tool_subsetting:
        # First so it trims the menu before the model call is assembled.
        middleware.insert(0, ToolSubsetMiddleware())
    return middleware
