from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware

from app.agent.middleware.context_window_middleware import ContextWindowMiddleware
from app.agent.middleware.history_cap_middleware import HistoryCapMiddleware
from app.agent.middleware.loop_guard_reset_middleware import LoopGuardResetMiddleware
from app.agent.middleware.tool_subset_middleware import ToolSubsetMiddleware
from app.agent.middleware.fast_path_middleware import FastPathMiddleware
from app.agent.middleware.watched_entities_middleware import WatchedEntitiesMiddleware
from app.config import Settings
from app.tools.adapter import LoopGuard


def build_middleware(
    settings: Settings,
    guard: LoopGuard,
    base_prompt: str,
    budget: int,
    fast_path=None,
    telemetry_tracer=None,
    telemetry_store_conn=None,
    rest=None,
    fast_path_health=None,
) -> list[AgentMiddleware]:
    middleware: list[AgentMiddleware] = [
        ContextWindowMiddleware(base_prompt, budget),
        LoopGuardResetMiddleware(guard),
    ]
    if settings.max_history_messages > 0:
        middleware.append(HistoryCapMiddleware(settings.max_history_messages))
    if settings.enable_tool_subsetting:
        # First so it trims the menu before the model call is assembled.
        middleware.insert(0, ToolSubsetMiddleware())
    # TelemetryMiddleware is inserted immediately before FastPath (outer to it).
    if telemetry_tracer is not None:
        from app.agent.middleware.telemetry_middleware import TelemetryMiddleware
        middleware.append(TelemetryMiddleware(telemetry_tracer))
    # FastPath sits inside every middleware except WatchedEntities.
    if fast_path is not None and settings.max_tier >= 2:
        backend, menu_provider = fast_path
        middleware.append(FastPathMiddleware(
            backend,
            menu_provider,
            settings.fast_path_confidence_threshold,
            tracer=telemetry_tracer,
            store_conn=telemetry_store_conn,
            health=fast_path_health,
        ))
    if settings.watched_entities and rest is not None:
        # Innermost: it appends the home_context block to the user's text for
        # the LLM only. ToolSubset routing and the fast-path classifier, both
        # outer to it, must see the raw message; a fast-path hit with the
        # block attached matches nothing, so every request fell through.
        middleware.append(WatchedEntitiesMiddleware(rest, settings.watched_entities))
    return middleware
