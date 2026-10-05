"""Carries out scheduled tasks whose trigger just happened.

A task with a saved action or reminder runs in Python: the action through the
trigger_action tool, the message through notify_user — the same adapter path
as an agent call, so the AI-switch gate, audit log and translation all apply,
but no LLM is involved. Only a task with neither falls back to an [EVENT] run
of the agent. Every task gets an outcome for the Notes page. Runs are
serialized so two events never act at once, and nothing here raises into the
listener.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agent.run_scope import scope_configurable
from app.tools import registry
from app.tools.adapter import LoopGuard, to_structured_tool

log = logging.getLogger("agent.events")

EVENTS_THREAD_ID = "events"
_NOTIFY_LIMIT = 500  # notify_user's message max_length
_ACTION_TOOLS = ("trigger_action", "notify_user")


@dataclass(frozen=True, slots=True)
class Trigger:
    kind: str  # "state" | "time"
    entity_id: str = ""
    from_state: str = ""
    to_state: str = ""
    at: str = ""  # time triggers: the clock reading, "HH:MM DD-MM-YYYY"


def _one_line(text: str) -> str:
    # A task is data. Collapsing whitespace keeps it on its own line, so it
    # can never start a line that looks like a harness instruction.
    return " ".join(text.split())


def describe_trigger(trigger: Trigger) -> str:
    if trigger.kind == "time":
        return f"[EVENT] Scheduled time {trigger.at} reached."
    old = trigger.from_state or "nothing"
    return f"[EVENT] {trigger.entity_id} changed {old} → {trigger.to_state}."


def build_event_message(trigger: Trigger, notes: list) -> str:
    """The LLM fallback's message: only tasks with no saved action/reminder."""
    lines = [describe_trigger(trigger), "Scheduled tasks for this event:"]
    for note in notes:
        lines.append(f'- #{note.id}: "{_one_line(note.instruction)}"')
    lines.append("Carry out each task now using your tools (list_actions, then trigger_action).")
    lines.append("When done, call notify_user once with a short summary of what you did.")
    # One thread holds every event run; earlier [EVENT] messages in its history
    # are already done and must never be acted on again (tasks are one-shot).
    lines.append("Act only on the tasks in this message; earlier [EVENT] messages are already handled.")
    lines.append("Do not ask questions; nobody is reading this thread live.")
    return "\n".join(lines)


class EventRunner:
    def __init__(self, get_agent: Callable[[], Any], settings, ctx) -> None:
        self._get_agent = get_agent  # a getter, so a swapped app.state.agent is honoured
        self._settings = settings
        self._ctx = ctx  # the app's ToolContext: same clients the agent's tools use
        self._lock = asyncio.Lock()

    async def run(self, trigger: Trigger, notes: list) -> None:
        async with self._lock:
            agent_tasks = []
            for note in notes:
                if note.action_entity_id or note.reminder:
                    await self._run_direct(note)
                else:
                    agent_tasks.append(note)
            if agent_tasks:
                await self._run_agent(trigger, agent_tasks)

    # ---------- direct path ----------
    def _config(self, note) -> dict:
        configurable = {"thread_id": EVENTS_THREAD_ID}
        # Suppression is decided here, sentence by sentence; notify_user itself
        # is never told to suppress on the direct path.
        configurable.update(scope_configurable(note.language, False))
        return {"configurable": configurable}

    async def _action_name(self, entity_id: str) -> str:
        try:
            state = await self._ctx.rest.get_state(entity_id)
        except Exception:
            return "the scheduled action"
        name = (state.get("attributes") or {}).get("friendly_name")
        return name or "the scheduled action"

    async def _run_direct(self, note) -> None:
        # A fresh guard per task: two tasks with the same action must both run.
        guard = LoopGuard()
        trigger_tool = to_structured_tool(registry.get("trigger_action"), self._ctx, guard)
        notify_tool = to_structured_tool(registry.get("notify_user"), self._ctx, guard)
        config = self._config(note)

        sentences = []
        outcome = "done"
        result = ""
        if note.action_entity_id:
            name = await self._action_name(note.action_entity_id)
            raw = await trigger_tool.ainvoke(
                {"entity_id": note.action_entity_id, "params": note.action_params}, config=config
            )
            envelope = json.loads(raw)
            if envelope.get("status") == "ok":
                result = f"{name} ✓"
                if self._settings.event_confirmations_enabled:
                    sentences.append(f"Done: {name}.")
            else:
                outcome = "failed"
                message = (envelope.get("error") or {}).get("message") or "unknown error"
                result = message
                sentences.append(f"Couldn't run {name}: {message}")
        if note.reminder:
            sentences.append(f"Reminder: {note.reminder}")
            if not note.action_entity_id:
                result = "reminder sent"

        if sentences:
            text = " ".join(sentences)[:_NOTIFY_LIMIT]
            sent = json.loads(await notify_tool.ainvoke({"message": text}, config=config))
            if sent.get("status") != "ok":
                log.warning("events: notification for task %s failed: %s", note.id, sent.get("error"))
        self._ctx.notes.record_outcome(note.id, outcome, result)
        log.info("events: task %s %s (%s)", note.id, outcome, result)

    # ---------- LLM fallback ----------
    async def _run_agent(self, trigger: Trigger, notes: list) -> None:
        note_ids = []
        for note in notes:
            note_ids.append(note.id)
        configurable = {"thread_id": EVENTS_THREAD_ID}
        # Oldest task first (store order): its language is the batch's language.
        configurable.update(
            scope_configurable(notes[0].language, not self._settings.event_confirmations_enabled)
        )
        try:
            state = await self._get_agent().ainvoke(
                {"messages": [{"role": "user", "content": build_event_message(trigger, notes)}]},
                config={
                    "configurable": configurable,
                    "recursion_limit": self._settings.recursion_limit,
                },
                context={"fast_path": False},
            )
        except Exception:
            log.exception("events: agent run failed tasks=%s", note_ids)
            for note_id in note_ids:
                self._ctx.notes.record_outcome(note_id, "failed", "agent run failed")
            return

        acted = _acting_tools(_this_run(state.get("messages") or []))
        if acted:
            outcome, result = "agent", ", ".join(acted)
        else:
            outcome, result = "no_action", "the agent did not act"
        for note_id in note_ids:
            self._ctx.notes.record_outcome(note_id, outcome, result)
        log.info("events: agent tasks %s %s (%s)", note_ids, outcome, result)


def _this_run(messages: list) -> list:
    """Messages after this run's [EVENT] message. ainvoke returns the whole
    events-thread history, and tool calls from earlier runs must not count."""
    start = 0
    for index, message in enumerate(messages):
        if getattr(message, "type", None) == "human":
            start = index + 1  # the last human message is this run's [EVENT]
    return messages[start:]


def _acting_tools(messages: list) -> list[str]:
    """'trigger_action ✓' / 'notify_user ✗' for every acting tool the run called."""
    acted = []
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        if message.name not in _ACTION_TOOLS:
            continue
        try:
            ok = json.loads(message.content).get("status") == "ok"
        except (ValueError, AttributeError):
            ok = False
        acted.append(f"{message.name} {'✓' if ok else '✗'}")
    return acted
