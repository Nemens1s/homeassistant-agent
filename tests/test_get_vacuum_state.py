import pytest
import httpx

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


def _state(entity_id, state, friendly_name=""):
    return {"entity_id": entity_id, "state": state, "attributes": {"friendly_name": friendly_name}}


class FakeRest:
    def __init__(self, vacuum_state, room_state):
        self._states = {
            "vacuum.roborock_qrevo_s": _state("vacuum.roborock_qrevo_s", vacuum_state, "Roborock Qrevo S"),
            "sensor.roborock_qrevo_s_current_room": _state("sensor.roborock_qrevo_s_current_room", room_state),
        }

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


def _ctx(vacuum_state="docked", room_state=""):
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(vacuum_state, room_state), ws=None)


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
    class NoRoomSensorRest:
        async def get_state(self, entity_id):
            if entity_id == "vacuum.roborock_qrevo_s":
                return _state(entity_id, "docked", "Roborock Qrevo S")
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "http://x"), response=httpx.Response(404)
            )

    ctx = ToolContext(settings=Settings(_env_file=None), rest=NoRoomSensorRest(), ws=None)
    result = await _defn().handler(_defn().params_model(), ctx)
    assert result.status == "ok"
    assert result.data["state"] == "docked"
    assert "current_room" not in result.data
