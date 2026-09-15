from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately

log = logging.getLogger("agent.factory")


def timestamped_system(base_prompt: str) -> SystemMessage:
    now = datetime.now().astimezone()
    return SystemMessage(
        f"{base_prompt}\n\nCurrent time: {now.isoformat(timespec='seconds')} ({now.tzname()})"
    )


def _drop_reasoning(messages: list) -> list:
    """Normalize AI messages before they're replayed in history.

    Thinking-enabled models (Qwen3.5, Claude extended-thinking, etc.) may store
    reasoning tokens in two places:
    - additional_kwargs["reasoning_content"] — stripped here
    - content as a list of {"type":"thinking",...} blocks — flattened to a plain
      string here, keeping only text blocks (tool_calls live in their own field)

    Most inference endpoints (including oMLX) reject both forms when they appear
    in replayed history.
    """
    result = []
    for m in messages:
        if not isinstance(m, AIMessage):
            result.append(m)
            continue
        updates: dict = {}
        if m.additional_kwargs.get("reasoning_content"):
            filtered_kwargs = {}
            for k, v in m.additional_kwargs.items():
                if k != "reasoning_content":
                    filtered_kwargs[k] = v
            updates["additional_kwargs"] = filtered_kwargs
        if isinstance(m.content, list):
            parts = []
            for block in m.content:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    continue
                if isinstance(block, dict):
                    parts.append(block.get("text", ""))
                else:
                    parts.append(str(block))
            updates["content"] = "".join(parts)
        if updates:
            m = m.model_copy(update=updates)
        result.append(m)
    return result


def trim_history(messages: list, max_tokens: int) -> list:
    trimmed = trim_messages(
        messages,
        max_tokens=max_tokens,
        token_counter=count_tokens_approximately,
        strategy="last",
        start_on="human",
        include_system=False,
        allow_partial=False,
    )
    return trimmed or messages[-1:]


class ContextWindowMiddleware(AgentMiddleware):
    """Non-destructive per-call trimming (the checkpointed history is left
    intact) + fresh timestamp in the system prompt."""

    def __init__(self, base_prompt: str, max_tokens: int):
        super().__init__()
        self._base_prompt = base_prompt
        self._max_tokens = max_tokens

    def _prepare(self, request):
        messages = _drop_reasoning(trim_history(list(request.messages), self._max_tokens))
        system = timestamped_system(self._base_prompt)
        try:
            return replace(request, messages=messages, system_message=system)
        except TypeError:  # pragma: no cover — future langchain versions may swap the dataclass for .override()
            return request.override(messages=messages, system_message=system)

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        prepared = self._prepare(request)
        try:
            return await handler(prepared)
        except Exception as exc:
            msg = str(exc)
            if "XML syntax error" in msg:
                log.warning("LLM produced malformed XML tool call: %s", exc)
                return AIMessage(
                    content="Your previous tool call contained malformed XML and could not be parsed. "
                    "Please retry with well-formed XML."
                )
            if "Extra data" in msg:
                log.warning("LLM produced trailing text after tool call JSON: %s", exc)
                return AIMessage(
                    content="Your previous tool call contained text after the JSON object. "
                    "Output only the JSON tool call with no trailing text."
                )
            raise
