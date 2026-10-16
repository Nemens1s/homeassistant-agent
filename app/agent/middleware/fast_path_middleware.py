"""Fast path as a model-call middleware. On a confident hit it short-circuits
the model call with a synthetic trigger_action tool call; the agent's tool
node runs it (gate + audit + loop guard apply). The follow-up step returns the
templated reply. Never fails a request: any error falls through to the LLM."""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import nullcontext

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.llm_health import is_fast_path_outage
from app.agent.middleware.tool_subset_middleware import latest_human_text
from app.telemetry import conventions
from app.telemetry.snapshots import menu_hash, register_menu

log = logging.getLogger("fast_path")

FASTPATH_PREFIX = "fastpath-"

# A request about the future ("once the vacuum starts, stop it") must reach the
# LLM, which saves a note — classifying it would run the action right now.
# Errs toward the LLM: a false positive costs latency, never correctness.
_DEFERRAL_CUE = re.compile(
    r"\b(when|once|after|if|remind\w*|later|tomorrow|tonight|next time|as soon as)\b"
    r"|\bin \d+\b|\bat \d{1,2}[:.]\d{2}\b",
    re.IGNORECASE,
)


def has_deferral_cue(text: str) -> bool:
    return bool(_DEFERRAL_CUE.search(text or ""))


def _reply_from_envelope(raw: str, name: str, params: dict | None = None) -> str:
    """Build the templated confirmation. `name` must be a human-readable label
    (the menu item's friendly name), never a raw HA entity/service id — raw ids
    in this text get shown to the user, sent to TTS and fed through translation,
    where a Latin-script id can also skew language detection of the whole reply."""
    try:
        env = json.loads(raw)
    except (ValueError, TypeError):
        return "Sorry, I couldn't run that automation."
    if env.get("status") == "ok":
        label = name or "that"
        if params:
            param_str = ", ".join(f"{k}: {v}" for k, v in params.items())
            return f"Done — triggered {label} ({param_str})."
        return f"Done — triggered {label}."
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
    def __init__(self, backend, menu_provider, threshold: float, tracer=None, store_conn=None,
                 health=None):
        super().__init__()
        self._backend = backend
        self._menu_provider = menu_provider
        self._threshold = threshold
        self._tracer = tracer
        self._store_conn = store_conn
        self._health = health  # ServiceHealth for the fast-path service, or None

    def _safe_set(self, span, key, value):
        """Set a span attribute without raising — keeps telemetry fail-open.
        A None span (telemetry off) is a no-op."""
        if span is None:
            return
        try:
            span.set_attribute(key, value)
        except Exception:
            pass

    def _note_failure(self, exc: Exception) -> None:
        """Passive health signal: a refused/timed-out classify marks the
        service down so the next requests skip it without waiting."""
        if self._health is not None and is_fast_path_outage(exc):
            self._health.mark_down(exc)

    def _record_skip(self, reason: str) -> None:
        """A classify span that only carries why the fast path was skipped."""
        if self._tracer is None:
            return
        try:
            with self._tracer.start_as_current_span(conventions.SPAN_CLASSIFY) as span:
                self._safe_set(span, conventions.GOSLING_FP_SKIP_REASON, reason)
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
                prev = messages[-2]
                prev_args = prev.tool_calls[0]["args"]
                params = prev_args.get("params") or {}
                name = prev.additional_kwargs.get("fastpath_name") or ""
            except (IndexError, KeyError, AttributeError, TypeError):
                params = {}
                name = ""
            return AIMessage(content=_reply_from_envelope(last.content, name, params))

        # First step of a turn → classify.
        if isinstance(last, HumanMessage):
            if _opted_out(request):
                self._record_skip("disabled")
                return await handler(request)
            if self._health is not None and self._health.is_down:
                self._record_skip("fast_path_down")
                return await handler(request)
            if has_deferral_cue(latest_human_text(messages)):
                self._record_skip("deferred")
                return await handler(request)

            result = await self._classify(last.content)
            if result is not None:
                decision, menu = result
                if decision.entity_id is not None and decision.confidence >= self._threshold:
                    # Fall through when required params are missing — the LLM must resolve them.
                    item = next((i for i in menu.items if i.entity_id == decision.entity_id), None)
                    if item:
                        required = item.parameters.get("required") or []
                        if required and not all(k in decision.arguments for k in required):
                            return await handler(request)
                    name = item.name if item else ""
                    return self._synthetic_call(decision.entity_id, decision.arguments, name)

        return await handler(request)

    def _classify_span(self):
        """The classify span, or a no-op context yielding None when telemetry
        is off or the tracer fails — the classification must never be lost."""
        if self._tracer is None:
            return nullcontext(None)
        try:
            return self._tracer.start_as_current_span(conventions.SPAN_CLASSIFY)
        except Exception:
            log.debug("fast path telemetry span creation failed; continuing without telemetry")
            return nullcontext(None)

    def _record_menu(self, span, menu) -> None:
        if span is None:
            return
        try:
            self._safe_set(span, conventions.GOSLING_FP_MENU_HASH, menu_hash(menu))
            if self._store_conn is not None:
                register_menu(self._store_conn, menu)
        except Exception:
            pass

    async def _classify(self, message: str):
        """Fetch the menu and classify the message.

        Returns:
            A (Decision, Menu) tuple, or None when the request should fall
            through to the LLM (empty menu or an error).
        """
        with self._classify_span() as span:
            self._safe_set(span, conventions.GOSLING_FP_BACKEND, self._backend.name)
            self._safe_set(span, conventions.GOSLING_FP_THRESHOLD, self._threshold)

            try:
                menu = await self._menu_provider.get()
            except Exception:
                log.exception("fast path error fetching menu; falling through to agent")
                self._safe_set(span, conventions.GOSLING_FP_SKIP_REASON, "error")
                return None

            if not menu.items:
                self._safe_set(span, conventions.GOSLING_FP_SKIP_REASON, "empty_menu")
                return None
            self._record_menu(span, menu)

            try:
                decision = await self._backend.classify(message, menu)
            except Exception as exc:
                self._note_failure(exc)
                log.exception("fast path error; falling through to agent")
                self._safe_set(span, conventions.GOSLING_FP_SKIP_REASON, "error")
                return None

            accepted = decision.entity_id is not None and decision.confidence >= self._threshold
            self._safe_set(span, conventions.GOSLING_FP_ENTITY_ID, decision.entity_id or "")
            self._safe_set(span, conventions.GOSLING_FP_CONFIDENCE, decision.confidence)
            self._safe_set(span, conventions.GOSLING_FP_ACCEPTED, accepted)
            return decision, menu

    def _synthetic_call(self, entity_id: str, arguments: dict | None = None,
                        name: str = "") -> AIMessage:
        call_id = FASTPATH_PREFIX + uuid.uuid4().hex
        return AIMessage(
            content="",
            tool_calls=[{"name": "trigger_action",
                         "args": {"entity_id": entity_id, "params": arguments or {}},
                         "id": call_id}],
            response_metadata={"model_name": self._backend.name, "fast_path": True},
            additional_kwargs={"fastpath_entity_id": entity_id, "fastpath_name": name},
        )
