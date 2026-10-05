from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.agent.run_scope import RunScope, use_scope
from app.config import Settings
from app.memory.store import NoteStore
from app.tools import registry
from app.tools.context import ToolContext

VACUUM = "vacuum.roborock_qrevo_s"
MODULES = (
    "app.tools.memory.schedule_on_state_change",
    "app.tools.memory.schedule_at_time",
    "app.tools.memory.list_scheduled",
    "app.tools.memory.cancel_scheduled",
)


STATUS = "sensor.roborock_qrevo_s_status"
STATUS_OPTIONS = [
    "charging", "washing_the_mop", "cleaning", "segment_cleaning",
    "spot_cleaning", "returning_home", "idle",
]


class FakeRest:
    def __init__(self, clock="09:32 05-10-2026"):
        self.clock = clock

    async def get_state(self, entity_id):
        if self.clock is None:
            raise ConnectionError("down")
        if entity_id == STATUS:
            return {"entity_id": entity_id, "state": "charging",
                    "attributes": {"device_class": "enum", "options": STATUS_OPTIONS}}
        return {"entity_id": entity_id, "state": self.clock, "attributes": {}}


class FakeLang:
    def __init__(self, translations=None):
        self.translations = translations or {}

    async def to_english(self, text):
        if text in self.translations:
            return self.translations[text], "ru"
        return text, "en"

    async def from_english(self, text, language):
        return text


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(MODULES)
    yield
    registry._reset_for_tests()


@pytest.fixture
def store():
    s = NoteStore("")
    yield s
    s.close()


def _ctx(store, rest=None, lang=None, **overrides):
    values = {"watched_entities": [VACUUM]}
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    return ToolContext(
        settings=settings, rest=rest or FakeRest(), ws=None, notes=store, lang=lang or FakeLang()
    )


async def _call(name, ctx, **args):
    defn = registry.get(name)
    return await defn.handler(defn.params_model(**args), ctx)


def _pending(store):
    return store.list_pending(datetime.now(timezone.utc))


async def test_schedule_on_state_change_stores_note_with_scope(store):
    with use_scope(RunScope(language="ru", thread_id="t7")):
        result = await _call(
            "schedule_on_state_change", _ctx(store),
            entity_id=VACUUM, to_state="cleaning",
            instruction="Stop the vacuum and send it to the dock.", kind="action",
        )
    assert result.status == "ok"
    assert result.data["expires_in_hours"] == 24
    note = _pending(store)[0]
    assert note.id == result.data["id"]
    assert (note.entity_id, note.to_state, note.kind) == (VACUUM, "cleaning", "action")
    assert note.language == "ru"
    assert note.source_thread_id == "t7"


async def test_schedule_on_state_change_rejects_unwatched_entity(store):
    result = await _call(
        "schedule_on_state_change", _ctx(store),
        entity_id="light.kitchen", instruction="Turn it off.", kind="action",
    )
    assert result.error_code == "entity_not_watched"
    assert result.data == {"watched": [VACUUM]}
    assert _pending(store) == []


async def test_schedule_on_state_change_feature_disabled(store):
    result = await _call(
        "schedule_on_state_change", _ctx(store, watched_entities=[]),
        entity_id=VACUUM, instruction="x", kind="action",
    )
    assert result.error_code == "feature_disabled"


async def test_schedule_on_state_change_without_store():
    result = await _call(
        "schedule_on_state_change", _ctx(None),
        entity_id=VACUUM, instruction="x", kind="action",
    )
    assert result.error_code == "notes_unavailable"


async def test_schedule_on_state_change_clamps_ttl(store):
    result = await _call(
        "schedule_on_state_change", _ctx(store),
        entity_id=VACUUM, instruction="x", kind="action", expires_in_hours=1000,
    )
    assert result.data["expires_in_hours"] == 168


async def test_instruction_is_stored_in_english_with_original(store):
    lang = FakeLang({"Останови пылесос.": "Stop the vacuum."})
    await _call(
        "schedule_on_state_change", _ctx(store, lang=lang),
        entity_id=VACUUM, instruction="Останови пылесос.", kind="action",
    )
    note = _pending(store)[0]
    assert note.instruction == "Stop the vacuum."
    assert note.instruction_original == "Останови пылесос."


async def test_event_note_schema_lists_watched_entities(store):
    defn = registry.get("schedule_on_state_change")
    model = defn.dynamic_params(_ctx(store))
    model(entity_id=VACUUM, instruction="x", kind="action")
    with pytest.raises(ValidationError):
        model(entity_id="light.kitchen", instruction="x", kind="action")


async def test_schedule_at_time_at(store):
    result = await _call(
        "schedule_at_time", _ctx(store), at="18:00", instruction="Call mum.", kind="reminder",
    )
    assert result.data["fire_at"] == "18:00 05-10-2026"
    note = _pending(store)[0]
    assert note.fire_at_local == "2026-10-05 18:00"
    assert note.expires_at_local == "2026-10-05 20:00"  # default 120 min grace


async def test_schedule_at_time_in_minutes(store):
    result = await _call(
        "schedule_at_time", _ctx(store), in_minutes=30, instruction="Check the oven.", kind="reminder",
    )
    assert result.data["fire_at"] == "10:02 05-10-2026"


async def test_schedule_at_time_past_date(store):
    result = await _call(
        "schedule_at_time", _ctx(store), at="08:00 05-10-2026", instruction="x", kind="reminder",
    )
    assert result.error_code == "in_past"


async def test_schedule_at_time_needs_exactly_one_time(store):
    result = await _call("schedule_at_time", _ctx(store), instruction="x", kind="reminder")
    assert result.error_code == "invalid_params"


async def test_schedule_at_time_clock_unreadable(store):
    result = await _call(
        "schedule_at_time", _ctx(store, rest=FakeRest(clock=None)),
        at="18:00", instruction="x", kind="reminder",
    )
    assert result.error_code == "clock_unavailable"


async def test_schedule_at_time_feature_disabled(store):
    result = await _call(
        "schedule_at_time", _ctx(store, clock_entity=""), at="18:00", instruction="x", kind="reminder",
    )
    assert result.error_code == "feature_disabled"


async def test_list_and_cancel(store):
    ctx = _ctx(store)
    saved = await _call(
        "schedule_on_state_change", ctx, entity_id=VACUUM, to_state="cleaning", instruction="Stop it.", kind="action",
    )
    listed = await _call("list_scheduled", ctx)
    rows = listed.data["rows"]
    assert rows[0]["id"] == saved.data["id"]
    assert rows[0]["when"] == f"{VACUUM} → cleaning"
    cancelled = await _call("cancel_scheduled", ctx, id=saved.data["id"])
    assert cancelled.status == "ok"
    again = await _call("cancel_scheduled", ctx, id=saved.data["id"])
    assert again.error_code == "not_found"


async def test_memory_tools_are_read_tier():
    for name in ("schedule_on_state_change", "schedule_at_time", "list_scheduled", "cancel_scheduled"):
        assert int(registry.get(name).tier) == 1


def _status_ctx(store):
    return _ctx(store, watched_entities=[VACUUM, STATUS])


async def test_ui_label_resolves_to_raw_state(store):
    result = await _call(
        "schedule_on_state_change", _status_ctx(store),
        entity_id=STATUS, to_state="Segment cleaning", instruction="Stop it.", kind="action",
    )
    assert result.data["to_state"] == ["segment_cleaning"]
    assert _pending(store)[0].to_state == "segment_cleaning"


async def test_partial_state_expands_to_every_matching_option(store):
    result = await _call(
        "schedule_on_state_change", _status_ctx(store),
        entity_id=STATUS, to_state="cleaning", instruction="Stop it.", kind="action",
    )
    expected = ["cleaning", "segment_cleaning", "spot_cleaning"]
    assert result.data["to_state"] == expected
    now = datetime.now(timezone.utc)
    assert len(store.match_state(STATUS, "segment_cleaning", now)) == 1


async def test_unknown_state_lists_valid_options(store):
    result = await _call(
        "schedule_on_state_change", _status_ctx(store),
        entity_id=STATUS, to_state="vacuuming", instruction="Stop it.", kind="action",
    )
    assert result.error_code == "invalid_state"
    assert result.data == {"valid_states": STATUS_OPTIONS}
    assert _pending(store) == []


async def test_entity_without_options_keeps_state_as_given(store):
    result = await _call(
        "schedule_on_state_change", _ctx(store),
        entity_id=VACUUM, to_state="cleaning", instruction="Stop it.", kind="action",
    )
    assert result.data["to_state"] == ["cleaning"]
