import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeWS:
    def __init__(self):
        self.registries = {
            "config/area_registry/list": [
                {"area_id": "kitchen", "name": "Kitchen"},
                {"area_id": "bedroom", "name": "Bedroom"},
            ],
            "config/device_registry/list": [
                {"id": "dev1", "area_id": "kitchen", "name_by_user": None, "name": "Hue Bulb"},
                {"id": "dev2", "area_id": None, "name_by_user": "Odd Sensor", "name": "Sensor X"},
            ],
            "config/entity_registry/list": [
                {"entity_id": "light.kitchen", "area_id": None, "device_id": "dev1"},
                {"entity_id": "sensor.odd", "area_id": None, "device_id": "dev2"},
                {"entity_id": "light.bedroom_lamp", "area_id": "bedroom", "device_id": None},
            ],
        }

    async def request_cached(self, msg_type, ttl=60.0):
        return self.registries[msg_type]


class FakeRest:
    def __init__(self):
        self.states = [
            {"entity_id": "automation.night_lights", "state": "on",
             "attributes": {"friendly_name": "Night lights", "id": "1234",
                            "last_triggered": "2026-07-12T22:00:00+00:00"}},
            {"entity_id": "automation.yaml_one", "state": "off",
             "attributes": {"friendly_name": "Yaml one"}},
        ]

    async def list_states(self):
        return self.states

    async def get_automation_config(self, automation_id):
        assert automation_id == "1234"
        return {"alias": "Night lights", "trigger": [{"platform": "sun"}], "action": []}


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all((
        "app.tools.read.get_areas_and_devices",
        "app.tools.read.get_automations",
    ))
    yield
    registry._reset_for_tests()


def _ctx(ws=None):
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=ws)


async def test_topology_groups_by_area():
    defn = registry.get("get_areas_and_devices")
    result = await defn.handler(defn.params_model(), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    kitchen = result.data["areas"]["Kitchen"]
    assert kitchen["devices"] == ["Hue Bulb"]
    assert kitchen["entities"] == ["light.kitchen"]
    bedroom = result.data["areas"]["Bedroom"]
    assert bedroom["entities"] == ["light.bedroom_lamp"]
    assert result.data["unassigned"]["entities"] == ["sensor.odd"]
    assert result.data["unassigned"]["devices"] == ["Odd Sensor"]


async def test_topology_without_ws():
    defn = registry.get("get_areas_and_devices")
    result = await defn.handler(defn.params_model(), _ctx(ws=None))
    assert result.status == "error"
    assert result.error_code == "ws_unavailable"


async def test_automations_list():
    defn = registry.get("get_automations")
    result = await defn.handler(defn.params_model(), _ctx())
    assert result.status == "ok"
    rows = result.data["rows"]
    assert rows[0]["entity_id"] == "automation.night_lights"
    assert rows[0]["last_triggered"] == "2026-07-12T22:00:00+00:00"


async def test_automation_detail_fetches_config():
    defn = registry.get("get_automations")
    result = await defn.handler(
        defn.params_model(entity_id="automation.night_lights"), _ctx()
    )
    assert result.status == "ok"
    assert result.data["trigger"] == [{"platform": "sun"}]


async def test_automation_detail_yaml_defined():
    defn = registry.get("get_automations")
    result = await defn.handler(
        defn.params_model(entity_id="automation.yaml_one"), _ctx()
    )
    assert result.status == "ok"
    assert "not retrievable" in result.data["note"]


async def test_automation_detail_unknown():
    defn = registry.get("get_automations")
    result = await defn.handler(
        defn.params_model(entity_id="automation.nope"), _ctx()
    )
    assert result.status == "error"
    assert result.error_code == "entity_not_found"
    assert "automation.night_lights" in result.data["did_you_mean"]
