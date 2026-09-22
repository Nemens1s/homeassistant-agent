from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from app.agent.middleware.context_window_middleware import ContextWindowMiddleware
from app.agent.middleware.history_cap_middleware import HistoryCapMiddleware
from app.agent.middleware.loop_guard_reset_middleware import LoopGuardResetMiddleware
from app.agent.middleware.tool_subset_middleware import ToolSubsetMiddleware
from app.config import Settings
from app.fast_path.middleware import FastPathMiddleware
from app.tools.adapter import LoopGuard


def build_middleware(settings: Settings, guard: LoopGuard, base_prompt: str, budget: int, fast_path=None) -> list[AgentMiddleware]:
    middleware: list[AgentMiddleware] = [
        ContextWindowMiddleware(base_prompt, budget),
        LoopGuardResetMiddleware(guard),
    ]
    if settings.max_history_messages > 0:
        middleware.append(HistoryCapMiddleware(settings.max_history_messages))
    if settings.enable_tool_subsetting:
        # First so it trims the menu before the model call is assembled.
        middleware.insert(0, ToolSubsetMiddleware())
    # Task 12 will insert TelemetryMiddleware here (immediately before FastPath).
    # FastPath is always appended last (innermost) so it short-circuits first.
    if fast_path is not None and settings.max_tier >= 2:
        backend, menu_provider = fast_path
        middleware.append(FastPathMiddleware(backend, menu_provider, settings.needle_confidence_threshold))
    return middleware
