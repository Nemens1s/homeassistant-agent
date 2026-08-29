"""FastPathRouter: the pre-agent hop. On a confident hit it invokes the EXISTING
trigger_automation StructuredTool (so the AI-actions gate + audit fire exactly as
for the agent) and returns a reply. Otherwise returns None -> caller runs the
agent. Never raises: any failure degrades to the agent."""

from __future__ import annotations

import json
import logging

log = logging.getLogger("needle")


class FastPathRouter:
    def __init__(self, backend, menu_provider, trigger_tool, threshold: float):
        self._backend = backend
        self._menu_provider = menu_provider
        self._trigger_tool = trigger_tool
        self._threshold = threshold

    async def try_fast_path(self, message: str, thread_id: str) -> str | None:
        try:
            menu = await self._menu_provider.get()
            if not menu.items:
                return None
            decision = await self._backend.classify(message, menu)
            print(f"Needle's decision {decision}")
            if decision.entity_id is None or decision.confidence < self._threshold:
                return None
            raw = await self._trigger_tool.ainvoke(
                {"entity_id": decision.entity_id},
                config={"configurable": {"thread_id": thread_id}},
            )
        except Exception:
            log.exception("needle fast path error; falling through to agent")
            return None
        return _reply_from_envelope(raw, decision.entity_id)


def _reply_from_envelope(raw: str, entity_id: str) -> str | None:
    try:
        env = json.loads(raw)
    except (ValueError, TypeError):
        return None  # malformed (shouldn't happen) -> fall through
    if env.get("status") == "ok":
        return f"Done — triggered {entity_id}."
    error = env.get("error") or {}
    message = error.get("message")
    if message:
        return message
    return "Sorry, I couldn't run that automation."
