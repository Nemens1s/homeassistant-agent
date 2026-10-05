"""Runs the agent headlessly for notes whose trigger just happened.

The event message is English and identical whatever the confirmations toggle
says: suppression is decided here and travels in the run scope, which only
notify_user reads. Runs are serialized so two events never act at once, and
a failing run never escapes into the listener.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agent.run_scope import scope_configurable

log = logging.getLogger("agent.events")

EVENTS_THREAD_ID = "events"


@dataclass(frozen=True, slots=True)
class Trigger:
    kind: str  # "state" | "time"
    entity_id: str = ""
    from_state: str = ""
    to_state: str = ""
    at: str = ""  # time triggers: the clock reading, "HH:MM DD-MM-YYYY"


def _one_line(text: str) -> str:
    # A note is data. Collapsing whitespace keeps it on its own line, so it
    # can never start a line that looks like a harness instruction.
    return " ".join(text.split())


def describe_trigger(trigger: Trigger) -> str:
    if trigger.kind == "time":
        return f"[EVENT] Scheduled time {trigger.at} reached."
    old = trigger.from_state or "nothing"
    return f"[EVENT] {trigger.entity_id} changed {old} → {trigger.to_state}."


def build_event_message(trigger: Trigger, notes: list) -> str:
    has_action = False
    has_reminder = False
    lines = [describe_trigger(trigger), "Scheduled tasks for this event:"]
    for note in notes:
        lines.append(f'- #{note.id} ({note.kind}): "{_one_line(note.instruction)}"')
        if note.kind == "reminder":
            has_reminder = True
        else:
            has_action = True
    if has_action:
        lines.append("Carry out each action task now using your tools (list_actions, then trigger_action).")
    if has_reminder:
        lines.append("For each reminder task, deliver the reminder to the user with notify_user.")
    if has_action:
        lines.append("When done, call notify_user once with a short summary of what you did.")
    # One thread holds every event run; earlier [EVENT] messages in its history
    # are already done and must never be acted on again (notes are one-shot).
    lines.append("Act only on the tasks in this message; earlier [EVENT] messages are already handled.")
    lines.append("Do not ask questions; nobody is reading this thread live.")
    return "\n".join(lines)


def should_suppress(notes: list, settings) -> bool:
    """Confirmations off suppresses notify_user — unless a reminder is in the
    batch, because then the notification IS the requested action."""
    if settings.event_confirmations_enabled:
        return False
    for note in notes:
        if note.kind == "reminder":
            return False
    return True


class EventRunner:
    def __init__(self, get_agent: Callable[[], Any], settings) -> None:
        self._get_agent = get_agent  # a getter, so a swapped app.state.agent is honoured
        self._settings = settings
        self._lock = asyncio.Lock()

    async def run(self, trigger: Trigger, notes: list) -> None:
        note_ids = []
        for note in notes:
            note_ids.append(note.id)
        configurable = {"thread_id": EVENTS_THREAD_ID}
        # Oldest note first (store order): its language is the batch's language.
        configurable.update(
            scope_configurable(notes[0].language, should_suppress(notes, self._settings))
        )
        message = build_event_message(trigger, notes)
        async with self._lock:
            try:
                await self._get_agent().ainvoke(
                    {"messages": [{"role": "user", "content": message}]},
                    config={
                        "configurable": configurable,
                        "recursion_limit": self._settings.recursion_limit,
                    },
                    context={"fast_path": False},
                )
            except Exception:
                log.exception("events: run failed notes=%s", note_ids)
