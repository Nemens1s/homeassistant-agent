"""The strict action matcher as an always-on tier, ahead of the fast path.

A message that plainly names one action ("lights on", "stop vacuum") is
answered with no Needle call and no LLM call: a synthetic trigger_action
tool call that the agent's tool node runs (gate + audit + loop guard apply),
then the templated reply. Stricter than offline mode: no did-you-mean list,
no deferral text, no action with required params. Anything else falls
through silently. Never fails a request: any error falls through too."""

from __future__ import annotations

import logging
import time
from contextlib import nullcontext

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from app.agent.middleware.fast_path_middleware import (
    has_deferral_cue,
    is_synthetic_result,
    opted_out,
    synthetic_trigger_call,
    templated_reply,
)
from app.agent.middleware.tool_subset_middleware import latest_human_text
from app.degraded.formatters import display_action_name
from app.degraded.matcher import match_action
from app.telemetry import conventions

log = logging.getLogger("action_match")

ACTION_MATCH_PREFIX = "actionmatch-"
# The "model" of the synthetic response; telemetry counts the path by it.
ACTION_MATCH_MODEL = conventions.ACTION_MATCH_MODEL

_ROWS_TTL_S = 60


class ActionRowsProvider:
    """list_actions rows, cached for a short TTL. `fetch` is an async
    callable returning the list_actions ToolResult."""

    def __init__(self, fetch, ttl_s: int = _ROWS_TTL_S):
        self._fetch = fetch
        self._ttl_s = ttl_s
        self._rows: list[dict] | None = None
        self._fetched_at = 0.0

    async def get(self) -> list[dict]:
        now = time.monotonic()
        if self._rows is not None and (now - self._fetched_at) < self._ttl_s:
            return self._rows
        result = await self._fetch()
        rows = []
        if result.status == "ok":
            rows = (result.data or {}).get("rows") or []
        self._rows = rows
        self._fetched_at = now
        return rows


class ActionMatchMiddleware(AgentMiddleware):
    def __init__(self, rows_provider, tracer=None):
        super().__init__()
        self._rows_provider = rows_provider
        self._tracer = tracer

    async def awrap_model_call(self, request, handler):
        messages = request.messages
        last = messages[-1] if messages else None

        # Step after an action-match tool call → templated reply.
        if is_synthetic_result(last, ACTION_MATCH_PREFIX):
            return templated_reply(messages)

        if not isinstance(last, HumanMessage) or opted_out(request):
            return await handler(request)
        text = latest_human_text(messages)
        if has_deferral_cue(text):
            return await handler(request)

        winner = await self._match(text)
        if winner is None:
            return await handler(request)
        name = display_action_name(winner.get("name", ""), winner.get("entity_id", ""))
        return synthetic_trigger_call(ACTION_MATCH_PREFIX, ACTION_MATCH_MODEL,
                                      winner["entity_id"], {}, name)

    async def _match(self, text: str) -> dict | None:
        """The one action *text* plainly names, or None to fall through."""
        try:
            rows = await self._rows_provider.get()
        except Exception:
            log.warning("action match: could not list actions; falling through", exc_info=True)
            return None
        match = match_action(text, rows)
        if match.winner is None:
            return None
        # The LLM must fill required script fields.
        required = (match.winner.get("params") or {}).get("required") or []
        if required:
            return None
        self._record_hit(match)
        return match.winner

    def _record_hit(self, match) -> None:
        """A classify span like the fast path's, so the Metrics tab can count
        action-match hits. Telemetry never fails the request."""
        if self._tracer is None:
            return
        try:
            span_context = self._tracer.start_as_current_span(conventions.SPAN_CLASSIFY)
        except Exception:
            span_context = nullcontext(None)
        try:
            with span_context as span:
                if span is None:
                    return
                span.set_attribute(conventions.GOSLING_FP_BACKEND, ACTION_MATCH_MODEL)
                span.set_attribute(conventions.GOSLING_FP_ENTITY_ID, match.winner.get("entity_id", ""))
                span.set_attribute(conventions.GOSLING_FP_CONFIDENCE, match.winner_score)
                span.set_attribute(conventions.GOSLING_FP_ACCEPTED, True)
        except Exception:
            pass
