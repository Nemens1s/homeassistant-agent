from __future__ import annotations

from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware

from app.agent.tool_router import select_tools


def latest_human_text(messages: list) -> str:
    """The most recent user message as plain text — the routing signal. Stays
    constant across a run's model calls (the human turn doesn't change while the
    agent loops), so the tool subset is stable within a run."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "human":
            content = m.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # multimodal: concatenate text parts
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        parts.append(p.get("text", ""))
                    else:
                        parts.append(str(p))
                return " ".join(parts)
    return ""


class ToolSubsetMiddleware(AgentMiddleware):
    """Narrow the offered tool menu to the latest user message (see tool_router).
    Availability only — the model still chooses among what's offered."""

    def _prepare(self, request):
        text = latest_human_text(request.messages)
        if not text:
            return request
        tools = select_tools(list(request.tools), text)
        if not tools or len(tools) == len(request.tools):
            return request  # nothing to trim
        try:
            return replace(request, tools=tools)
        except TypeError:  # pragma: no cover — future langchain may drop dataclass
            return request.override(tools=tools)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))
