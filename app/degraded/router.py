"""The degraded-mode front door, consulted by both chat endpoints.

Slash commands are always handled here. Free text is handled here only while
the LLM is down: deferral guard -> fast path (if its service is up) -> fuzzy
action matcher -> "LLM offline" message. Returns None to let the agent
answer. Replies are English and never touch the conversation thread.
"""

from __future__ import annotations

import logging

from app.agent.llm_health import is_fast_path_outage
from app.agent.middleware.fast_path_middleware import has_deferral_cue
from app.degraded import Reply
from app.degraded.commands import (
    cmd_actions,
    cmd_cancel,
    cmd_help,
    cmd_notes,
    cmd_run,
    list_action_rows,
    run_action_row,
)
from app.degraded.formatters import display_action_name, format_did_you_mean, format_trigger
from app.degraded.matcher import match_action
from app.degraded.tools import ToolRunner

log = logging.getLogger("agent.degraded")

DEFERRAL_REPLY = "I can't schedule things while my language model is offline. Try again later."


def offline_message(llm_health) -> str:
    since = ""
    if llm_health is not None:
        since = f" (since {llm_health.since_hhmm()})"
    return (
        f"My language model is offline{since}, so I can only handle simple commands "
        "right now. Try /actions, /run <name>, or /help."
    )


class CommandRouter:
    def __init__(self, ctx, enabled, llm_health, fast_path=None, fp_health=None) -> None:
        self._ctx = ctx
        self._enabled = enabled
        self._llm_health = llm_health
        self._fast_path = fast_path  # (backend, menu_provider) or None
        self._fp_health = fp_health

    @staticmethod
    def is_command(text: str) -> bool:
        return text.strip().startswith("/")

    async def handle(self, text: str, thread_id: str) -> Reply | None:
        stripped = text.strip()
        runner = ToolRunner(self._ctx, self._enabled, thread_id)
        if stripped.startswith("/"):
            reply_text = await self._command(stripped, runner)
            return Reply(reply_text, "command", runner.calls)
        if self._llm_health is not None and self._llm_health.is_down:
            reply_text = await self._fallback(stripped, runner)
            return Reply(reply_text, "degraded", runner.calls)
        return None

    async def _command(self, text: str, runner) -> str:
        parts = text[1:].split(maxsplit=1)
        name = ""
        if parts:
            name = parts[0].lower()
        arg = ""
        if len(parts) > 1:
            arg = parts[1]
        if name == "actions":
            return await cmd_actions(arg, runner)
        if name == "run":
            return await cmd_run(arg, runner)
        if name == "notes":
            return await cmd_notes(arg, runner)
        if name == "cancel":
            return await cmd_cancel(arg, runner)
        return await cmd_help(arg, runner, self._llm_health)

    async def _fallback(self, text: str, runner) -> str:
        # A request about later must never run now (same rule as the fast path).
        if has_deferral_cue(text):
            return DEFERRAL_REPLY
        fast = await self._try_fast_path(text, runner)
        if fast is not None:
            return fast
        if runner.has("list_actions") and runner.has("trigger_action"):
            rows = await list_action_rows(runner)
            if isinstance(rows, list):
                match = match_action(text, rows)
                if match.winner is not None:
                    return await run_action_row(match.winner, {}, runner)
                if match.candidates:
                    return format_did_you_mean(match.candidates, rows)
        return offline_message(self._llm_health)

    async def _try_fast_path(self, text: str, runner) -> str | None:
        if self._fast_path is None or not runner.has("trigger_action"):
            return None
        if self._fp_health is not None and self._fp_health.is_down:
            return None
        backend, menu_provider = self._fast_path
        try:
            menu = await menu_provider.get()
            if not menu.items:
                return None
            decision = await backend.classify(text, menu)
        except Exception as exc:
            if self._fp_health is not None and is_fast_path_outage(exc):
                self._fp_health.mark_down(exc)
            log.warning("degraded: fast path failed (%s); trying the matcher", exc)
            return None
        threshold = self._ctx.settings.fast_path_confidence_threshold
        if decision.entity_id is None or decision.confidence < threshold:
            return None
        item = None
        for candidate in menu.items:
            if candidate.entity_id == decision.entity_id:
                item = candidate
                break
        if item is None:
            return None
        required = item.parameters.get("required") or []
        for key in required:
            if key not in decision.arguments:
                return None
        envelope = await runner.run(
            "trigger_action", {"entity_id": item.entity_id, "params": decision.arguments},
        )
        return format_trigger(envelope, display_action_name(item.name, item.entity_id),
                              decision.arguments)
