import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {
        "entity_id": "vacuum.roborock_qrevo_s",
        "state": "docked",
        "attributes": {"friendly_name": "Roborock Qrevo S"},
    },
    {
        "entity_id": "sensor.roborock_battery",
        "state": "85",
        "attributes": {"friendly_name": "Roborock Battery"},
    },
    {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen Light"},
    },
]


class FakeRest:
    async def list_states(self):
        return STATES


class FakeWS:
    async def request_cached(self, msg_type, ttl=60.0):
        if msg_type == "config/area_registry/list":
            return [{"area_id": "living_room", "name": "Living Room"}]
        if msg_type == "config/device_registry/list":
            return [{"id": "dev_vacuum", "area_id": "living_room"}]
        if msg_type == "config/entity_registry/list":
            return [
                {"entity_id": "vacuum.roborock_qrevo_s", "area_id": None, "device_id": "dev_vacuum"},
                {"entity_id": "sensor.roborock_battery", "area_id": None, "device_id": None},
                {"entity_id": "light.kitchen", "area_id": None, "device_id": None},
            ]
        return []


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.search_entities",))
    yield
    registry._reset_for_tests()


def _ctx(ws=None):
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=ws)


def _defn():
    return registry.get("search_entities")


async def test_search_matches_friendly_name():
    result = await _defn().handler(_defn().params_model(query="roborock"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert "vacuum.roborock_qrevo_s" in ids
    assert "sensor.roborock_battery" in ids
    assert "light.kitchen" not in ids


async def test_search_matches_entity_id():
    result = await _defn().handler(_defn().params_model(query="kitchen"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen"]


async def test_search_case_insensitive():
    result = await _defn().handler(_defn().params_model(query="ROBOROCK"), _ctx())
    assert result.status == "ok"
    assert len(result.data["rows"]) == 2


async def test_search_multi_word_query_matches_any_word():
    # "roborock cleaner" — "cleaner" matches nothing, "roborock" matches two entities
    result = await _defn().handler(_defn().params_model(query="roborock cleaner"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert "vacuum.roborock_qrevo_s" in ids
    assert "sensor.roborock_battery" in ids


async def test_search_no_match_returns_empty():
    result = await _defn().handler(_defn().params_model(query="nonexistent"), _ctx())
    assert result.status == "ok"
    assert result.data["rows"] == []


async def test_search_includes_state():
    result = await _defn().handler(_defn().params_model(query="roborock qrevo"), _ctx())
    assert result.status == "ok"
    row = result.data["rows"][0]
    assert row["state"] == "docked"
    assert row["friendly_name"] == "Roborock Qrevo S"


async def test_search_includes_area_when_ws_available():
    result = await _defn().handler(_defn().params_model(query="roborock qrevo"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    row = result.data["rows"][0]
    assert row["area"] == "Living Room"


async def test_search_omits_area_without_ws():
    result = await _defn().handler(_defn().params_model(query="roborock qrevo"), _ctx(ws=None))
    assert result.status == "ok"
    row = result.data["rows"][0]
    assert "area" not in row
