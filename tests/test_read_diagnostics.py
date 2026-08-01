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

    async def get_logbook(self, start_time, end_time=None):
        return [
            {"when": "2026-07-12T21:00:00+00:00", "name": "Kitchen Light",
             "message": "turned on", "entity_id": "light.kitchen"},
        ]

    async def get_error_log(self):
        return "\n".join(f"line {i}" for i in range(1, 101))


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all((
        "app.tools.read.get_history",
        "app.tools.read.get_logbook",
        "app.tools.read.get_error_log",
    ))
    yield
    registry._reset_for_tests()


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=None)


async def test_get_history_compacts_rows():
    defn = registry.get("get_history")
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", start_time="2026-07-12T00:00:00"),
        _ctx(),
    )
    assert result.status == "ok"
    assert result.data["rows"] == [
        {"state": "off", "at": "2026-07-12T20:00:00+00:00"},
        {"state": "on", "at": "2026-07-12T21:00:00+00:00"},
    ]


async def test_get_logbook_rows():
    defn = registry.get("get_logbook")
    result = await defn.handler(
        defn.params_model(start_time="2026-07-12T00:00:00"), _ctx()
    )
    assert result.status == "ok"
    row = result.data["rows"][0]
    assert row == {
        "at": "2026-07-12T21:00:00+00:00", "name": "Kitchen Light",
        "message": "turned on", "entity_id": "light.kitchen",
    }


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


async def test_get_history_normalizes_empty_end_time_to_none():
    captured = {}

    class CapturingRest:
        async def get_history(self, entity_id, start_time, end_time=None):
            captured["end_time"] = end_time
            return []

    defn = registry.get("get_history")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=CapturingRest(), ws=None)
    await defn.handler(
        defn.params_model(entity_id="light.kitchen", start_time="2026-07-12T00:00:00", end_time=""),
        ctx,
    )
    assert captured["end_time"] is None


async def test_get_logbook_normalizes_empty_end_time_to_none():
    captured = {}

    class CapturingRest:
        async def get_logbook(self, start_time, end_time=None):
            captured["end_time"] = end_time
            return []

    defn = registry.get("get_logbook")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=CapturingRest(), ws=None)
    await defn.handler(defn.params_model(start_time="2026-07-12T00:00:00", end_time=""), ctx)
    assert captured["end_time"] is None
