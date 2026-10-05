from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.events.listener import EventListener, build_subscription
from app.events.runner import Trigger
from app.memory.store import NoteStore

VACUUM = "vacuum.roborock_qrevo_s"
CLOCK = "sensor.europe_tallinn"


class FakeWS:
    def __init__(self):
        self.subscriptions = []

    async def subscribe(self, message, callback):
        self.subscriptions.append((message, callback))


class FakeSwitchRest:
    """Point-reads of the AI-actions switch."""

    def __init__(self, state="on", fail=False):
        self.state = state
        self.fail = fail
        self.reads = 0

    async def get_state(self, entity_id):
        self.reads += 1
        if self.fail:
            raise ConnectionError("HA down")
        return {"entity_id": entity_id, "state": self.state}


class FakeRunner:
    def __init__(self, store):
        self.store = store
        self.runs = []

    async def run(self, trigger, notes):
        statuses = []
        for note in self.store.list_recent():
            statuses.append(note.status)
        ids = []
        for note in notes:
            ids.append(note.id)
        self.runs.append({"trigger": trigger, "ids": ids, "statuses": statuses})


@pytest.fixture
def store():
    s = NoteStore("")
    yield s
    s.close()


def _settings(**overrides):
    values = {"watched_entities": [VACUUM], "clock_entity": CLOCK}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _event(entity_id, old, new):
    from_state = None if old is None else {"state": old}
    return {"variables": {"trigger": {
        "platform": "state", "entity_id": entity_id,
        "from_state": from_state, "to_state": {"state": new},
    }}}


def _vacuum_note(store, to_state="cleaning"):
    now = datetime.now(timezone.utc)
    return store.add_state_note(
        entity_id=VACUUM, to_state=to_state, instruction="Stop the vacuum.",
        instruction_original=None, kind="action", language="en", tags=[],
        expires_at=now + timedelta(hours=24), now=now, source_thread_id="t1",
    )


def _time_note(store, fire_at):
    return store.add_time_note(
        fire_at_local=fire_at, expires_at_local=fire_at + timedelta(minutes=120),
        instruction="Call mum.", instruction_original=None, kind="reminder",
        language="en", tags=[], now=datetime.now(timezone.utc), source_thread_id="t1",
    )


def _listener(store, rest=None, **overrides):
    ws = FakeWS()
    runner = FakeRunner(store)
    rest = rest or FakeSwitchRest()
    return EventListener(ws, store, runner, _settings(**overrides), rest), ws, runner


def test_subscription_ignores_attribute_changes():
    message = build_subscription([VACUUM])
    assert message["type"] == "subscribe_trigger"
    assert message["trigger"] == {"platform": "state", "entity_id": [VACUUM], "to": None}


async def test_start_subscribes_watched_and_clock(store):
    listener, ws, _ = _listener(store)
    await listener.start()
    assert len(ws.subscriptions) == 1
    assert ws.subscriptions[0][0]["trigger"]["entity_id"] == [VACUUM, CLOCK]


async def test_clock_listed_twice_is_subscribed_once(store):
    listener, ws, runner = _listener(store, watched_entities=[VACUUM, CLOCK])
    await listener.start()
    assert ws.subscriptions[0][0]["trigger"]["entity_id"] == [VACUUM, CLOCK]
    _vacuum_note(store, to_state=None)  # matches any vacuum change, never a clock tick
    await listener.handle_event(_event(CLOCK, "09:31 05-10-2026", "09:32 05-10-2026"))
    assert runner.runs == []  # a clock tick is never a state-note trigger


async def test_start_without_anything_to_watch(store):
    listener, ws, _ = _listener(store, watched_entities=[], clock_entity="")
    await listener.start()
    assert ws.subscriptions == []


async def test_unmatched_event_runs_nothing(store):
    _vacuum_note(store, to_state="cleaning")
    listener, _, runner = _listener(store)
    await listener.handle_event(_event(VACUUM, "cleaning", "returning"))
    assert runner.runs == []


async def test_matched_event_marks_fired_before_running(store):
    note_id = _vacuum_note(store)
    listener, _, runner = _listener(store)
    await listener.handle_event(_event(VACUUM, "docked", "cleaning"))
    run = runner.runs[0]
    assert run["ids"] == [note_id]
    assert run["statuses"] == ["fired"]  # already fired when the agent starts
    assert run["trigger"] == Trigger(
        kind="state", entity_id=VACUUM, from_state="docked", to_state="cleaning"
    )
    await listener.handle_event(_event(VACUUM, "docked", "cleaning"))
    assert len(runner.runs) == 1  # one-shot


@pytest.mark.parametrize("old, new", [("unavailable", "cleaning"), ("cleaning", "unknown")])
async def test_unavailable_transitions_are_ignored(store, old, new):
    _vacuum_note(store, to_state=None)
    listener, _, runner = _listener(store)
    await listener.handle_event(_event(VACUUM, old, new))
    assert runner.runs == []


async def test_clock_tick_fires_due_time_notes(store):
    note_id = _time_note(store, datetime(2026, 10, 5, 18, 0))
    listener, _, runner = _listener(store)
    await listener.handle_event(_event(CLOCK, "17:59 05-10-2026", "18:00 05-10-2026"))
    assert runner.runs[0]["ids"] == [note_id]
    assert runner.runs[0]["trigger"] == Trigger(kind="time", at="18:00 05-10-2026")


async def test_clock_tick_before_time_runs_nothing(store):
    _time_note(store, datetime(2026, 10, 5, 18, 0))
    listener, _, runner = _listener(store)
    await listener.handle_event(_event(CLOCK, "17:58 05-10-2026", "17:59 05-10-2026"))
    assert runner.runs == []


async def test_garbage_clock_state_is_ignored(store):
    _time_note(store, datetime(2026, 10, 5, 18, 0))
    listener, _, runner = _listener(store)
    await listener.handle_event(_event(CLOCK, "17:59 05-10-2026", "not a time"))
    assert runner.runs == []


@pytest.mark.parametrize("rest", [
    FakeSwitchRest(state="off"),
    FakeSwitchRest(state="unavailable"),
    FakeSwitchRest(fail=True),
])
async def test_switch_off_or_unreadable_keeps_notes_pending(store, rest):
    note_id = _vacuum_note(store)
    listener, _, runner = _listener(store, rest=rest)
    await listener.handle_event(_event(VACUUM, "docked", "cleaning"))
    assert runner.runs == []  # no LLM call
    pending = store.list_pending(datetime.now(timezone.utc))
    assert [note.id for note in pending] == [note_id]  # can still fire later


async def test_note_fires_once_switch_is_back_on(store):
    rest = FakeSwitchRest(state="off")
    note_id = _vacuum_note(store)
    listener, _, runner = _listener(store, rest=rest)
    await listener.handle_event(_event(VACUUM, "docked", "cleaning"))
    rest.state = "on"
    await listener.handle_event(_event(VACUUM, "docked", "cleaning"))
    assert runner.runs[0]["ids"] == [note_id]


async def test_unmatched_events_do_not_read_the_switch(store):
    rest = FakeSwitchRest()
    listener, _, _ = _listener(store, rest=rest)
    await listener.handle_event(_event(VACUUM, "docked", "cleaning"))
    await listener.handle_event(_event(CLOCK, "17:58 05-10-2026", "17:59 05-10-2026"))
    assert rest.reads == 0


async def test_empty_switch_setting_disables_the_gate(store):
    rest = FakeSwitchRest(state="off")
    note_id = _vacuum_note(store)
    listener, _, runner = _listener(store, rest=rest, ai_actions_switch="")
    await listener.handle_event(_event(VACUUM, "docked", "cleaning"))
    assert runner.runs[0]["ids"] == [note_id]
