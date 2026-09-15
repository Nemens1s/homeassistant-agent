import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    async def get_history(self, entity_id, start_time, end_time=None):
        return [[
            {"state": "off", "last_changed": "2026-07-12T20:00:00+00:00"},
            {"state": "on", "last_changed": "2026-07-12T21:00:00+00:00"},
        ]]

    async def get_logbook(self, start_time, end_time=None, entity_id=None):
        entries = [
            {"when": "2026-07-12T21:00:00+00:00", "name": "Kitchen Light",
             "message": "turned on", "entity_id": "light.kitchen"},
            {"when": "2026-07-12T22:00:00+00:00", "name": "Air Purifier",
             "message": "turned on", "entity_id": "fan.air_purifier"},
        ]
        if entity_id:
            entries = [e for e in entries if e["entity_id"] == entity_id]
        return entries

    async def get_error_log(self):
        return "\n".join(f"line {i}" for i in range(1, 101))


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all((
        "app.tools.read.get_history",
        "app.tools.read.get_activity",
        "app.tools.read.get_error_log",
    ))
    yield
    registry._reset_for_tests()


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=None)


async def test_get_history_compacts_rows():
    defn = registry.get("get_history")
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", range="last_24h"),
        _ctx(),
    )
    assert result.status == "ok"
    assert result.data["rows"] == [
        {"state": "off", "at": "2026-07-12T20:00:00+00:00"},
        {"state": "on", "at": "2026-07-12T21:00:00+00:00"},
    ]


async def test_get_history_defaults_range_to_last_24h():
    defn = registry.get("get_history")
    # range is optional — a small model may omit it entirely.
    result = await defn.handler(defn.params_model(entity_id="light.kitchen"), _ctx())
    assert result.status == "ok"


async def test_get_history_rejects_freetext_timestamp():
    defn = registry.get("get_history")
    with pytest.raises(Exception):  # ValidationError — timestamps are no longer accepted
        defn.params_model(entity_id="light.kitchen", range="2026-07-12T00:00:00")


async def test_get_activity_rows():
    defn = registry.get("get_activity")
    result = await defn.handler(defn.params_model(range="last_hour"), _ctx())
    assert result.status == "ok"
    assert len(result.data["rows"]) == 2
    assert result.data["rows"][0]["entity_id"] == "light.kitchen"


async def test_get_activity_entity_filter():
    defn = registry.get("get_activity")
    result = await defn.handler(
        defn.params_model(range="last_hour", entity_id="fan.air_purifier"), _ctx()
    )
    assert result.status == "ok"
    rows = result.data["rows"]
    assert len(rows) == 1
    assert rows[0]["entity_id"] == "fan.air_purifier"


async def test_get_error_log_tails_50_lines():
    defn = registry.get("get_error_log")
    result = await defn.handler(defn.params_model(), _ctx())
    assert result.status == "ok"
    assert len(result.data["lines"]) == 50
    assert result.data["lines"][-1] == "line 100"
    assert result.data["total_lines"] == 100


async def test_get_history_empty_data_returns_empty_rows():
    class EmptyRest:
        async def get_history(self, entity_id, start_time, end_time=None):
            return []

    defn = registry.get("get_history")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=EmptyRest(), ws=None)
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", start_time="2026-07-12T00:00:00"), ctx
    )
    assert result.status == "ok"
    assert result.data["rows"] == []
    assert result.data["total"] == 0


async def test_get_history_open_range_passes_none_end_time():
    """Open-ended ranges (last_24h) resolve end_time to None → HA defaults to now."""
    captured = {}

    class CapturingRest:
        async def get_history(self, entity_id, start_time, end_time=None):
            captured["start_time"] = start_time
            captured["end_time"] = end_time
            return []

    defn = registry.get("get_history")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=CapturingRest(), ws=None)
    await defn.handler(
        defn.params_model(entity_id="light.kitchen", range="last_24h"), ctx
    )
    assert captured["end_time"] is None
    assert "T" in captured["start_time"]  # a resolved ISO8601 timestamp


async def test_get_activity_yesterday_passes_closed_window():
    """'yesterday' is a closed day, so end_time is a concrete timestamp, not None."""
    captured = {}

    class CapturingRest:
        async def get_logbook(self, start_time, end_time=None, entity_id=None):
            captured["start_time"] = start_time
            captured["end_time"] = end_time
            return []

    defn = registry.get("get_activity")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=CapturingRest(), ws=None)
    await defn.handler(defn.params_model(range="yesterday"), ctx)
    assert captured["end_time"] is not None
    assert "T" in captured["start_time"] and "T" in captured["end_time"]
