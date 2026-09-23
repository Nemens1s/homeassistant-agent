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

from app.telemetry import conventions
from app.telemetry.snapshots import menu_hash, register_menu

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
    def __init__(self, backend, menu_provider, threshold: float, tracer=None, store_conn=None):
        super().__init__()
        self._backend = backend
        self._menu_provider = menu_provider
        self._threshold = threshold
        self._tracer = tracer
        self._store_conn = store_conn

    def _safe_set(self, span, key, value):
        """Set a span attribute without raising — keeps telemetry fail-open."""
        try:
            span.set_attribute(key, value)
        except Exception:
            pass

    async def awrap_model_call(self, request, handler):
        messages = request.messages
        last = messages[-1] if messages else None

        # Step after a fast-path tool call → templated reply.
        if isinstance(last, ToolMessage) and str(last.tool_call_id).startswith(FASTPATH_PREFIX):
            # fastpath_entity_id does NOT survive onto the ToolMessage (LangGraph
            # constructs it from the tool result only); read the entity_id from the
            # preceding AIMessage's tool_calls args instead.
            try:
                entity_id = messages[-2].tool_calls[0]["args"]["entity_id"]
            except (IndexError, KeyError, AttributeError, TypeError):
                entity_id = ""
            return AIMessage(content=_reply_from_envelope(last.content, entity_id))

        # First step of a turn → classify.
        if isinstance(last, HumanMessage):
            if _opted_out(request):
                # Emit a disabled span when tracer is set, then fall through.
                if self._tracer is not None:
                    try:
                        with self._tracer.start_as_current_span(conventions.SPAN_CLASSIFY) as span:
                            self._safe_set(span, conventions.GOSLING_FP_SKIP_REASON, "disabled")
                    except Exception:
                        pass
                return await handler(request)

            decision = None
            result = await self._classify_with_telemetry(last.content, decision)
            if result is not None:
                # result is either a Decision (hit/miss) or a sentinel meaning fall-through
                if result == "fallthrough":
                    return await handler(request)
                # result is a Decision
                if result.entity_id is not None and result.confidence >= self._threshold:
                    return self._synthetic_call(result.entity_id)

        return await handler(request)

    async def _classify_with_telemetry(self, message: str, _unused):
        """Run menu fetch + classify, wrapped in a telemetry span if tracer is set.

        Returns:
            A Decision on success, or the string "fallthrough" when the request
            should fall through to the LLM (empty menu or backend error).
        """
        if self._tracer is None:
            # No telemetry — plain path identical to original behavior.
            try:
                menu = await self._menu_provider.get()
                if not menu.items:
                    return "fallthrough"
                return await self._backend.classify(message, menu)
            except Exception:
                log.exception("fast path error; falling through to agent")
                return "fallthrough"

        # Telemetry-instrumented path.
        # Fail-open: if the span context manager itself raises, fall back to the
        # plain classification so the hit is never lost.
        try:
            span_ctx = self._tracer.start_as_current_span(conventions.SPAN_CLASSIFY)
        except Exception:
            log.debug("fast path telemetry span creation failed; continuing without telemetry")
            try:
                menu = await self._menu_provider.get()
                if not menu.items:
                    return "fallthrough"
                return await self._backend.classify(message, menu)
            except Exception:
                log.exception("fast path error; falling through to agent")
                return "fallthrough"

        with span_ctx as span:
            try:
                self._safe_set(span, conventions.GOSLING_FP_BACKEND, self._backend.name)
                self._safe_set(span, conventions.GOSLING_FP_THRESHOLD, self._threshold)

                try:
                    menu = await self._menu_provider.get()
                except Exception:
                    log.exception("fast path error fetching menu; falling through to agent")
                    self._safe_set(span, conventions.GOSLING_FP_SKIP_REASON, "error")
                    return "fallthrough"

                if not menu.items:
                    self._safe_set(span, conventions.GOSLING_FP_SKIP_REASON, "empty_menu")
                    return "fallthrough"

                try:
                    mhash = menu_hash(menu)
                    self._safe_set(span, conventions.GOSLING_FP_MENU_HASH, mhash)
                except Exception:
                    pass

                if self._store_conn is not None:
                    try:
                        register_menu(self._store_conn, menu)
                    except Exception:
                        pass

                try:
                    decision = await self._backend.classify(message, menu)
                except Exception:
                    log.exception("fast path error; falling through to agent")
                    self._safe_set(span, conventions.GOSLING_FP_SKIP_REASON, "error")
                    return "fallthrough"

                entity_id = decision.entity_id or ""
                accepted = decision.entity_id is not None and decision.confidence >= self._threshold
                self._safe_set(span, conventions.GOSLING_FP_ENTITY_ID, entity_id)
                self._safe_set(span, conventions.GOSLING_FP_CONFIDENCE, decision.confidence)
                self._safe_set(span, conventions.GOSLING_FP_ACCEPTED, accepted)

                return decision

            except Exception:
                # Last-resort catch: telemetry raised unexpectedly. Fall through so
                # the agent still handles the request.
                log.debug("fast path telemetry error; falling through to agent")
                return "fallthrough"

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
