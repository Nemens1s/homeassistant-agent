import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {"entity_id": "sensor.sofija_iphone_battery", "state": "82",
     "attributes": {"friendly_name": "Sofija Nikolski's iPhone battery",
                    "device_class": "battery", "unit_of_measurement": "%"}},
    {"entity_id": "sensor.remote_battery", "state": "15",
     "attributes": {"friendly_name": "Living Room Remote battery",
                    "device_class": "battery", "unit_of_measurement": "%"}},
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen Light"}},
]


class FakeRest:
    def __init__(self, states=None):
        self._states = states if states is not None else STATES

    async def list_states(self):
        return self._states


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.get_battery_status",))
    yield
    registry._reset_for_tests()


def _ctx(states=None, name_map=None):
    settings = Settings(_env_file=None, person_name_map=name_map or [])
    return ToolContext(settings=settings, rest=FakeRest(states), ws=None)


def _defn():
    return registry.get("get_battery_status")


async def test_returns_only_battery_entities():
    result = await _defn().handler(_defn().params_model(), _ctx())
    assert result.status == "ok"
    ids = [b["entity_id"] for b in result.data["batteries"]]
    assert "sensor.sofija_iphone_battery" in ids
    assert "light.kitchen" not in ids


async def test_name_aliases_present_when_configured():
    name_map = [
        {"ha_name": "Sofija Nikolski", "name": "Sofija"},
        {"ha_name": "Sonja", "name": "Sofija"},
    ]
    result = await _defn().handler(_defn().params_model(), _ctx(name_map=name_map))
    assert result.status == "ok"
    assert result.data["name_aliases"] == {"Sofija": ["Sofija Nikolski", "Sonja"]}


async def test_name_aliases_absent_when_no_aliases():
    result = await _defn().handler(_defn().params_model(), _ctx())
    assert result.status == "ok"
    assert "name_aliases" not in result.data
