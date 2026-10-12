import pytest
import httpx

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


def _state(entity_id, state, friendly_name=""):
    return {"entity_id": entity_id, "state": state, "attributes": {"friendly_name": friendly_name}}


class WS:
    connected = True

    def __init__(self, entities):
        self._entities = entities

    async def request_cached(self, cmd):
        assert cmd == "config/entity_registry/list"
        return self._entities


_ENTITIES = [
    {"entity_id": "vacuum.a", "device_id": "d1"},
    {"entity_id": "sensor.a_current_room", "device_id": "d1"},
]


class FakeRest:
    def __init__(self, vacuum_state, room_state):
        self._states = {
            "vacuum.a": _state("vacuum.a", vacuum_state, "Roborock Qrevo S"),
            "sensor.a_current_room": _state("sensor.a_current_room", room_state),
        }

    async def list_states(self):
        return list(self._states.values())

    async def get_state(self, entity_id):
        if entity_id not in self._states:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "http://x"), response=httpx.Response(404)
            )
        return self._states[entity_id]


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.get_vacuum_state",))
    yield
    registry._reset_for_tests()


def _ctx(vacuum_state="docked", room_state="", ws=None):
    if ws is None:
        ws = WS(_ENTITIES)
    return ToolContext(
        settings=Settings(_env_file=None), rest=FakeRest(vacuum_state, room_state), ws=ws
    )


def _ctx_with_states(states):
    class EmptyRest:
        async def list_states(self):
            return states

    return ToolContext(settings=Settings(_env_file=None), rest=EmptyRest(), ws=WS([]))


def _defn():
    return registry.get("get_vacuum_state")


async def test_returns_vacuum_state_when_docked():
    result = await _defn().handler(_defn().params_model(), _ctx(vacuum_state="docked"))
    assert result.status == "ok"
    assert result.data["state"] == "docked"


async def test_returns_current_room_when_cleaning():
    result = await _defn().handler(_defn().params_model(), _ctx(vacuum_state="cleaning", room_state="Kitchen"))
    assert result.status == "ok"
    assert result.data["state"] == "cleaning"
    assert result.data["current_room"] == "Kitchen"


async def test_omits_current_room_when_empty():
    result = await _defn().handler(_defn().params_model(), _ctx(vacuum_state="docked", room_state=""))
    assert result.status == "ok"
    assert "current_room" not in result.data


async def test_omits_current_room_when_unknown():
    result = await _defn().handler(_defn().params_model(), _ctx(vacuum_state="docked", room_state="unknown"))
    assert result.status == "ok"
    assert "current_room" not in result.data


async def test_still_returns_state_if_room_sensor_missing():
    ws = WS([{"entity_id": "vacuum.a", "device_id": "d1"}])
    ctx = _ctx(vacuum_state="docked", ws=ws)
    result = await _defn().handler(_defn().params_model(), ctx)
    assert result.status == "ok"
    assert result.data["state"] == "docked"
    assert "current_room" not in result.data


async def test_no_vacuum_returns_error_envelope():
    result = await _defn().handler(_defn().params_model(), _ctx_with_states([]))
    assert result.status == "error"
    assert result.error_code == "no_vacuum"
