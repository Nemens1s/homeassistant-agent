from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage


def current_time_line() -> str:
    # Minute precision: every model call inside one run (tool call → result →
    # answer) sends the same bytes, so llama.cpp's prompt cache still matches.
    now = datetime.now().astimezone()
    return f"Current time: {now.isoformat(timespec='minutes')} ({now.tzname()})"


def with_current_time(messages: list) -> list | None:
    """Copy of ``messages`` with the current time appended to the latest user
    turn, or None when there is no user turn to carry it."""
    index = None
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "type", None) == "human":
            index = i
            break
    if index is None:
        return None
    original = messages[index]
    line = current_time_line()
    if isinstance(original.content, str):
        content = original.content + "\n\n" + line
    elif isinstance(original.content, list):  # multimodal: add a text part
        content = list(original.content) + [{"type": "text", "text": line}]
    else:
        return None
    result = list(messages)
    result[index] = original.model_copy(update={"content": content})
    return result


class CurrentTimeMiddleware(AgentMiddleware):
    """Puts the current time at the END of the prompt (the latest user turn)
    for this model call only. In the system prompt it changed the prefix on
    every call, so llama.cpp re-processed the tool schemas and the whole
    history each time. The checkpointed messages are never changed."""

    def _prepare(self, request):
        messages = with_current_time(list(request.messages))
        if messages is not None:
            try:
                return replace(request, messages=messages)
            except TypeError:  # pragma: no cover — future langchain may drop dataclass
                return request.override(messages=messages)
        # No user turn: fall back to the system prompt so the time is never lost.
        base = ""
        if request.system_message is not None:
            base = request.system_message.content + "\n\n"
        system = SystemMessage(base + current_time_line())
        try:
            return replace(request, system_message=system)
        except TypeError:  # pragma: no cover
            return request.override(system_message=system)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))
