"""The strict action matcher as an always-on tier, ahead of the fast path.

A message that plainly names one action ("lights on", "stop vacuum") is
answered with no Needle call and no LLM call: a synthetic trigger_action
tool call that the agent's tool node runs (gate + audit + loop guard apply),
then the templated reply. The match is strict but not literal (the matcher's
rules: one clear winner whose name covers every word). It runs over every
action, and fires only when the winner's HA description carries the
EXACT-MATCH flag: a better unflagged match must win, not lose to a flagged
one that merely contains the words. Stricter than offline mode: no did-you-mean
list, no deferral text, no action that takes params. Anything else falls
through silently. Never fails a request: any error falls through too."""

from __future__ import annotations

import asyncio
import logging
import time

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
from app.degraded.formatters import display_action_name, sorted_action_rows
from app.degraded.matcher import is_question, match_action
from app.telemetry import conventions

log = logging.getLogger("action_match")

ACTION_MATCH_PREFIX = "actionmatch-"
# The "model" of the synthetic response; telemetry counts the path by it.
ACTION_MATCH_MODEL = conventions.ACTION_MATCH_MODEL

# After a failed listing, skip the tier this long instead of retrying HA on
# every turn.
_FAILURE_BACKOFF_S = 15


class ActionRowsProvider:
    """Matcher rows ({entity_id, name, params, exact_match}) from a
    MenuProvider of every action. The menu provider owns the TTL cache; this
    adds a lock (one fetch at a time) and a short backoff after a failure."""

    def __init__(self, menu_provider, backoff_s: float = _FAILURE_BACKOFF_S):
        self._menu_provider = menu_provider
        self._backoff_s = backoff_s
        self._failed_at: float | None = None
        self._lock = asyncio.Lock()

    def _backing_off(self) -> bool:
        if self._failed_at is None:
            return False
        return time.monotonic() - self._failed_at < self._backoff_s

    async def get(self) -> list[dict]:
        if self._backing_off():
            return []
        async with self._lock:
            # Checked again: a request queued behind a failed fetch must not
            # retry HA itself.
            if self._backing_off():
                return []
            try:
                menu = await self._menu_provider.get()
            except Exception as exc:
                self._failed_at = time.monotonic()
                log.warning("action match: could not list actions (%s); skipping for %ss",
                            exc, self._backoff_s)
                return []
        self._failed_at = None
        rows = []
        for item in menu.items:
            rows.append({"entity_id": item.entity_id, "name": item.name,
                         "params": item.parameters, "exact_match": item.exact_match})
        return sorted_action_rows(rows)


def _takes_params(row: dict) -> bool:
    """Any script field, optional or not: the fast path or the LLM fills it
    (an optional `room` comes from the request area there)."""
    params = row.get("params") or {}
    if params.get("properties"):
        return True
    return bool(params.get("required"))


class ActionMatchMiddleware(AgentMiddleware):
    def __init__(self, rows_provider):
        super().__init__()
        self._rows_provider = rows_provider

    async def awrap_model_call(self, request, handler):
        messages = request.messages
        last = messages[-1] if messages else None

        # Step after an action-match tool call → templated reply.
        if is_synthetic_result(last, ACTION_MATCH_PREFIX):
            return templated_reply(messages)

        if not isinstance(last, HumanMessage) or opted_out(request):
            return await handler(request)
        # Cheap checks first: neither of these can fire, so skip the listing.
        text = latest_human_text(messages)
        if has_deferral_cue(text) or is_question(text):
            return await handler(request)

        winner = await self._match(text)
        if winner is None:
            return await handler(request)
        name = display_action_name(winner.get("name", ""), winner.get("entity_id", ""))
        return synthetic_trigger_call(ACTION_MATCH_PREFIX, ACTION_MATCH_MODEL,
                                      winner["entity_id"], {}, name)

    async def _match(self, text: str) -> dict | None:
        """The one action *text* plainly names, or None to fall through."""
        rows = await self._rows_provider.get()
        match = match_action(text, rows)
        if match.winner is None or not match.winner.get("exact_match"):
            return None
        if _takes_params(match.winner):
            return None
        return match.winner
