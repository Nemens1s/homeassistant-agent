"""Fast path as a model-call middleware. On a confident hit it short-circuits
the model call with a synthetic trigger_automation tool call; the agent's tool
node runs it (gate + audit + loop guard apply). The follow-up step returns the
templated reply. Never fails a request: any error falls through to the LLM."""

from __future__ import annotations

import json
import logging
import uuid

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

log = logging.getLogger("fast_path")

FASTPATH_PREFIX = "fastpath-"


def _reply_from_envelope(raw: str, entity_id: str) -> str:
    try:
        env = json.loads(raw)
    except (ValueError, TypeError):
        return "Sorry, I couldn't run that automation."
    if env.get("status") == "ok":
        return f"Done — triggered {entity_id}."
    error = env.get("error") or {}
    message = error.get("message")
    if message:
        return message
    return "Sorry, I couldn't run that automation."


def _opted_out(request) -> bool:
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None)
    if context is None:
        return False
    if isinstance(context, dict):
        return context.get("fast_path") is False
    return getattr(context, "fast_path", True) is False


class FastPathMiddleware(AgentMiddleware):
    def __init__(self, backend, menu_provider, threshold: float):
        super().__init__()
        self._backend = backend
        self._menu_provider = menu_provider
        self._threshold = threshold

    async def awrap_model_call(self, request, handler):
        messages = request.messages
        last = messages[-1] if messages else None

        # Step after a fast-path tool call → templated reply.
        if isinstance(last, ToolMessage) and str(last.tool_call_id).startswith(FASTPATH_PREFIX):
            entity_id = last.additional_kwargs.get("fastpath_entity_id", "")
            return AIMessage(content=_reply_from_envelope(last.content, entity_id))

        # First step of a turn → classify.
        if isinstance(last, HumanMessage) and not _opted_out(request):
            try:
                menu = await self._menu_provider.get()
                if not menu.items:
                    return await handler(request)
                decision = await self._backend.classify(last.content, menu)
            except Exception:
                log.exception("fast path error; falling through to agent")
                return await handler(request)
            if decision.entity_id is not None and decision.confidence >= self._threshold:
                return self._synthetic_call(decision.entity_id)

        return await handler(request)

    def _synthetic_call(self, entity_id: str) -> AIMessage:
        call_id = FASTPATH_PREFIX + uuid.uuid4().hex
        return AIMessage(
            content="",
            tool_calls=[{"name": "trigger_automation",
                         "args": {"entity_id": entity_id},
                         "id": call_id}],
            response_metadata={"model_name": self._backend.name, "fast_path": True},
            additional_kwargs={"fastpath_entity_id": entity_id},
        )
