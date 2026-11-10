from __future__ import annotations

from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware

from app.agent.middleware.current_time_middleware import append_to_latest_user_turn
from app.agent.run_scope import RequestFacts, scope_from_running_graph


def request_context_line(facts: RequestFacts) -> str | None:
    """The line telling the model where the request came from, or None when
    nothing resolved. The area is worded as a default: a room the user names
    wins."""
    parts = []
    if facts.area_id is not None:
        parts.append(
            f'the request came from a device in area "{facts.area_name}" '
            f"(area_id: {facts.area_id})"
        )
    if facts.person_entity_id is not None:
        parts.append(f"speaker: {facts.person_name} ({facts.person_entity_id})")
    if not parts:
        return None
    line = "Request context: " + "; ".join(parts) + "."
    if facts.area_id is not None:
        line += " If the user names a room, use that room instead."
    return line


def with_request_context(messages: list, line: str) -> list | None:
    """Copy of ``messages`` with ``line`` on the latest user turn, or None."""
    return append_to_latest_user_turn(messages, line)


class RequestContextMiddleware(AgentMiddleware):
    """Puts the request's area and speaker (resolved at the HTTP edge, carried
    in the run's configurable) on the latest user turn for this model call
    only. The checkpointed messages and the system prompt are never changed."""

    def _prepare(self, request):
        line = request_context_line(scope_from_running_graph().request)
        if line is None:
            return request
        messages = with_request_context(list(request.messages), line)
        if messages is None:
            return request
        try:
            return replace(request, messages=messages)
        except TypeError:  # pragma: no cover — future langchain may drop dataclass
            return request.override(messages=messages)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))
