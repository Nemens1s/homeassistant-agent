"""Turns HA state changes into note firings.

One subscribe_trigger covers every watched entity plus the clock. Each event
is matched against the note store in SQL; only when notes match does the
agent run. Most events — every clock tick without a due note, every vacuum
state nobody left a note for — cost no LLM call at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.events.clock import format_clock, parse_clock
from app.events.runner import Trigger

log = logging.getLogger("agent.events")

# Restart noise: an entity going to or coming back from these is not a real
# transition. The clock catches up on its next tick (due_time uses "<=").
_IGNORED_STATES = ("unavailable", "unknown")


def build_subscription(entities: list[str]) -> dict:
    # "to": None = real state changes only; attribute-only updates are ignored.
    return {
        "type": "subscribe_trigger",
        "trigger": {"platform": "state", "entity_id": list(entities), "to": None},
    }


def _state_of(state_obj) -> str:
    return (state_obj or {}).get("state") or ""


class EventListener:
    def __init__(self, ws, notes, runner, settings, rest) -> None:
        self._ws = ws
        self._notes = notes
        self._runner = runner
        self._settings = settings
        self._rest = rest  # point-reads of the AI-actions switch

    def _entities(self) -> list[str]:
        entities = []
        for entity_id in self._settings.watched_entities:
            if entity_id not in entities:
                entities.append(entity_id)
        clock = self._settings.clock_entity
        if clock and clock not in entities:
            entities.append(clock)
        return entities

    async def start(self) -> None:
        entities = self._entities()
        if not entities or self._ws is None or self._notes is None:
            log.info("events: listener off (entities=%s notes=%s)", entities, self._notes is not None)
            return
        await self._ws.subscribe(build_subscription(entities), self.handle_event)
        log.info("events: watching %s", entities)

    async def handle_event(self, event: dict) -> None:
        trigger = (event.get("variables") or {}).get("trigger") or {}
        entity_id = trigger.get("entity_id") or ""
        old = _state_of(trigger.get("from_state"))
        new = _state_of(trigger.get("to_state"))
        if old in _IGNORED_STATES or new in _IGNORED_STATES:
            return
        if entity_id == self._settings.clock_entity:
            await self._on_clock(new)
        else:
            await self._on_state(entity_id, old, new)

    async def _on_clock(self, state: str) -> None:
        now_local = parse_clock(state)
        if now_local is None:
            return
        notes = self._notes.due_time(now_local)
        if not notes:
            return  # once a minute — not worth a log line
        await self._fire(Trigger(kind="time", at=format_clock(now_local)), notes)

    async def _on_state(self, entity_id: str, old: str, new: str) -> None:
        notes = self._notes.match_state(entity_id, new, datetime.now(timezone.utc))
        log.debug("event entity=%s from=%s to=%s matched=%d", entity_id, old, new, len(notes))
        if not notes:
            return
        trigger = Trigger(kind="state", entity_id=entity_id, from_state=old, to_state=new)
        await self._fire(trigger, notes)

    async def _ai_switch_on(self) -> bool:
        """The home-level AI switch, read fail-closed like the tool adapter's
        gate: only a readable "on" lets an event run. "" disables the gate."""
        switch = self._settings.ai_actions_switch
        if not switch:
            return True
        try:
            state = await self._rest.get_state(switch)
        except Exception:
            return False
        return state.get("state") == "on"

    async def _fire(self, trigger: Trigger, notes: list) -> None:
        note_ids = []
        for note in notes:
            note_ids.append(note.id)
        # Checked before marking fired: with the switch off the notes stay
        # pending (and may still fire before they expire) and no LLM runs.
        if not await self._ai_switch_on():
            log.info("events: AI actions switched off — notes %s left pending", note_ids)
            return
        # Fired BEFORE the run: a crash mid-run must not make a note fire twice.
        self._notes.mark_fired(note_ids, datetime.now(timezone.utc))
        log.info("events: firing notes=%s trigger=%s", note_ids, trigger)
        await self._runner.run(trigger, notes)
