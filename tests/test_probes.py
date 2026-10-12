from app.config import Settings
from app.tools.context import ToolContext
from app.tools.helpers.probes import any_entity, find_vacuum, room_sensor_for


class Rest:
    def __init__(self, states):
        self._states = states

    async def list_states(self):
        return self._states


class WS:
    connected = True

    def __init__(self, entities):
        self._entities = entities

    async def request_cached(self, cmd):
        assert cmd == "config/entity_registry/list"
        return self._entities


def _ctx(states, ws=None, **settings):
    return ToolContext(settings=Settings(_env_file=None, **settings), rest=Rest(states), ws=ws)


async def test_any_entity_by_prefix_and_device_class():
    states = [{"entity_id": "sensor.b", "attributes": {"device_class": "battery"}}]
    assert await any_entity(_ctx(states), device_class="battery") is True
    assert await any_entity(_ctx(states), prefix="vacuum.") is False


async def test_find_vacuum_override_then_first():
    states = [{"entity_id": "vacuum.z", "attributes": {}}, {"entity_id": "vacuum.a", "attributes": {}}]
    assert await find_vacuum(_ctx(states)) == "vacuum.a"
    assert await find_vacuum(_ctx(states, vacuum_entity="vacuum.z")) == "vacuum.z"
    assert await find_vacuum(_ctx([])) is None


async def test_room_sensor_found_via_same_device():
    ents = [
        {"entity_id": "vacuum.a", "device_id": "d1"},
        {"entity_id": "sensor.a_battery", "device_id": "d1"},
        {"entity_id": "sensor.a_current_room", "device_id": "d1"},
        {"entity_id": "sensor.other_current_room", "device_id": "d2"},
    ]
    assert await room_sensor_for(_ctx([], ws=WS(ents)), "vacuum.a") == "sensor.a_current_room"


async def test_room_sensor_none_without_ws():
    assert await room_sensor_for(_ctx([], ws=None), "vacuum.a") is None
