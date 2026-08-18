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
                {"id": "dev2", "area_id": "bedroom", "name_by_user": "My Sensor", "name": "Sensor X"},
                {"id": "dev3", "area_id": None, "name_by_user": None, "name": "Unassigned Hub"},
            ],
        }

    async def request_cached(self, msg_type, ttl=60.0):
        return self.registries[msg_type]


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.list_devices",))
    yield
    registry._reset_for_tests()


def _ctx(ws=None):
    return ToolContext(settings=Settings(_env_file=None), rest=None, ws=ws)


async def test_list_devices_all():
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    names = [d["name"] for d in result.data["devices"]]
    assert "Hue Bulb" in names
    assert "My Sensor" in names  # name_by_user takes priority
    assert "Unassigned Hub" in names
    assert result.data["total"] == 3


async def test_list_devices_area_filter():
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(area="Kitchen"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    names = [d["name"] for d in result.data["devices"]]
    assert names == ["Hue Bulb"]
    assert result.data["devices"][0]["area"] == "Kitchen"


async def test_list_devices_area_case_insensitive():
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(area="kitchen"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    assert result.data["devices"][0]["name"] == "Hue Bulb"


async def test_list_devices_name_search():
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(name="hub"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    assert result.data["devices"][0]["name"] == "Unassigned Hub"
    assert result.data["total"] == 1


async def test_list_devices_name_search_case_insensitive():
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(name="HUE"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    assert result.data["devices"][0]["name"] == "Hue Bulb"


async def test_list_devices_unknown_area():
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(area="Garage"), _ctx(ws=FakeWS()))
    assert result.status == "error"
    assert result.error_code == "area_not_found"
    assert "Kitchen" in result.data["available_areas"]


async def test_list_devices_without_ws():
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(), _ctx(ws=None))
    assert result.status == "error"
    assert result.error_code == "ws_unavailable"


async def test_list_devices_name_by_user_takes_priority():
    """name_by_user overrides the default name field."""
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(area="Bedroom"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    assert result.data["devices"][0]["name"] == "My Sensor"


async def test_list_devices_unassigned_has_empty_area():
    defn = registry.get("list_devices")
    result = await defn.handler(defn.params_model(name="Unassigned"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    assert result.data["devices"][0]["area"] == ""
