from datetime import datetime, timedelta, timezone

import pytest

from app.memory.store import NoteStore

NOW = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)
VACUUM = "vacuum.roborock_qrevo_s"


@pytest.fixture
def store():
    s = NoteStore("")
    yield s
    s.close()


def _state_note(store, entity_id=VACUUM, to_state="cleaning", kind="action", hours=24):
    return store.add_state_note(
        entity_id=entity_id,
        to_state=to_state,
        instruction="Stop the vacuum and send it to the dock.",
        instruction_original=None,
        kind=kind,
        language="en",
        tags=["vacuum"],
        expires_at=NOW + timedelta(hours=hours),
        now=NOW,
        source_thread_id="t1",
    )


def _time_note(store, fire_at, grace_minutes=120):
    return store.add_time_note(
        fire_at_local=fire_at,
        expires_at_local=fire_at + timedelta(minutes=grace_minutes),
        instruction="Call mum.",
        instruction_original="Позвони маме.",
        kind="reminder",
        language="ru",
        tags=[],
        now=NOW,
        source_thread_id="t1",
    )


def _ids(notes):
    ids = []
    for note in notes:
        ids.append(note.id)
    return ids


def test_state_note_matches_entity_and_state(store):
    note_id = _state_note(store)
    assert _ids(store.match_state(VACUUM, "cleaning", NOW)) == [note_id]
    assert store.match_state(VACUUM, "docked", NOW) == []
    assert store.match_state("vacuum.other", "cleaning", NOW) == []


def test_null_to_state_matches_any_change(store):
    note_id = _state_note(store, to_state=None)
    assert _ids(store.match_state(VACUUM, "returning", NOW)) == [note_id]


def test_to_state_matches_case_insensitively(store):
    note_id = _state_note(store, to_state="Cleaning")
    assert _ids(store.match_state(VACUUM, "cleaning", NOW)) == [note_id]


def test_state_note_expires(store):
    note_id = _state_note(store, hours=24)
    assert store.match_state(VACUUM, "cleaning", NOW + timedelta(hours=25)) == []
    statuses = {}
    for note in store.list_recent():
        statuses[note.id] = note.status
    assert statuses[note_id] == "expired"


def test_fired_note_is_not_matched_again(store):
    note_id = _state_note(store)
    store.mark_fired([note_id], NOW)
    assert store.match_state(VACUUM, "cleaning", NOW) == []
    fired = store.list_recent()[0]
    assert fired.status == "fired"
    assert fired.fired_at is not None


def test_cancel_only_pending(store):
    note_id = _state_note(store)
    assert store.cancel(note_id) is True
    assert store.cancel(note_id) is False
    assert store.list_pending(NOW) == []


def test_fired_note_cannot_be_cancelled(store):
    note_id = _state_note(store)
    store.mark_fired([note_id], NOW)
    assert store.cancel(note_id) is False
    assert store.list_recent()[0].status == "fired"


def test_time_note_due_and_catch_up(store):
    fire_at = datetime(2026, 10, 5, 18, 0)
    note_id = _time_note(store, fire_at)
    assert store.due_time(datetime(2026, 10, 5, 17, 59)) == []
    # a missed tick (reconnect) still fires on the next one
    assert _ids(store.due_time(datetime(2026, 10, 5, 18, 5))) == [note_id]


def test_time_note_expires_after_grace(store):
    fire_at = datetime(2026, 10, 5, 18, 0)
    _time_note(store, fire_at, grace_minutes=120)
    assert store.due_time(datetime(2026, 10, 5, 20, 1)) == []
    assert store.list_recent()[0].status == "expired"


def test_round_trip_fields(store):
    note_id = _time_note(store, datetime(2026, 10, 5, 18, 0))
    note = store.list_pending(NOW)[0]
    assert note.id == note_id
    assert note.trigger_kind == "time"
    assert note.fire_at_local == "2026-10-05 18:00"
    assert note.instruction_original == "Позвони маме."
    assert note.language == "ru"
    assert note.tags == []
    assert note.to_dict()["kind"] == "reminder"


def test_list_recent_is_newest_first(store):
    first = _state_note(store)
    second = _state_note(store)
    assert _ids(store.list_recent()) == [second, first]


def test_unusable_path_degrades(tmp_path):
    bad = NoteStore(str(tmp_path / "missing-dir" / "notes.sqlite"))
    assert bad.available is False
    assert _state_note(bad) is None
    assert bad.match_state(VACUUM, "cleaning", NOW) == []
    assert bad.cancel(1) is False
