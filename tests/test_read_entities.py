import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen Light", "brightness": 200},
     "last_changed": "2026-07-13T10:00:00+00:00"},
    {"entity_id": "light.bedroom", "state": "off",
     "attributes": {"friendly_name": "Bedroom Light"},
     "last_changed": "2026-07-13T09:00:00+00:00"},
    {"entity_id": "sensor.temp", "state": "21.5",
     "attributes": {"friendly_name": "Temperature"},
     "last_changed": "2026-07-13T10:30:00+00:00"},
]


class FakeRest:
    async def get_state(self, entity_id):
        for s in STATES:
            if s["entity_id"] == entity_id:
                return s
        raise AssertionError("test should not request unknown ids directly")

    async def list_states(self):
        return STATES


class FakeWS:
    def __init__(self):
        self.registries = {
            "config/area_registry/list": [{"area_id": "kitchen", "name": "Kitchen"}],
            "config/device_registry/list": [{"id": "dev1", "area_id": "kitchen"}],
            "config/entity_registry/list": [
                {"entity_id": "light.kitchen", "area_id": None, "device_id": "dev1"},
                {"entity_id": "light.bedroom", "area_id": None, "device_id": None},
            ],
        }

    async def request_cached(self, msg_type, ttl=60.0):
        return self.registries[msg_type]


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(
        ("app.tools.read.get_entity_state", "app.tools.read.list_entities")
    )
    yield
    registry._reset_for_tests()


def _ctx(ws=None):
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=ws)


async def test_get_entity_state():
    defn = registry.get("get_entity_state")
    result = await defn.handler(defn.params_model(entity_id="light.kitchen"), _ctx())
    assert result.status == "ok"
    assert result.data["state"] == "on"
    assert result.data["attributes"]["brightness"] == 200


async def test_list_entities_domain_filter():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(domain="light"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen", "light.bedroom"]
    assert result.data["total"] == 2


async def test_list_entities_area_filter():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(area="Kitchen"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen"]  # via device dev1 in area kitchen


async def test_list_entities_unknown_area():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(area="Garage"), _ctx(ws=FakeWS()))
    assert result.status == "error"
    assert result.error_code == "area_not_found"
    assert result.data["available_areas"] == ["Kitchen"]


async def test_list_entities_area_without_ws():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(area="Kitchen"), _ctx(ws=None))
    assert result.status == "error"
    assert result.error_code == "ws_unavailable"
