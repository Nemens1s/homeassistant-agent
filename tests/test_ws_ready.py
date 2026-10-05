from types import SimpleNamespace

from app.config import Settings
from app.ha.websocket import ws_is_ready
from app.tools import registry
from app.tools.context import ToolContext


def test_none_is_not_ready():
    assert ws_is_ready(None) is False


def test_disconnected_client_is_not_ready():
    assert ws_is_ready(SimpleNamespace(connected=False)) is False


def test_connected_client_is_ready():
    assert ws_is_ready(SimpleNamespace(connected=True)) is True


def test_fake_without_flag_counts_as_ready():
    assert ws_is_ready(object()) is True


async def test_list_devices_degrades_while_reconnecting():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.list_devices",))
    defn = registry.get("list_devices")
    ctx = ToolContext(
        settings=Settings(_env_file=None), rest=None, ws=SimpleNamespace(connected=False)
    )
    result = await defn.handler(defn.params_model(), ctx)
    registry._reset_for_tests()
    assert result.error_code == "ws_unavailable"


class _HangingWS:
    """A client still reconnecting: any registry call would wait out the timeout."""

    connected = False

    def __init__(self):
        self.calls = []

    async def request_cached(self, msg_type, ttl=60.0):
        self.calls.append(msg_type)
        raise AssertionError("must not query a disconnected websocket")


class _BatteryRest:
    async def list_states(self):
        return [{"entity_id": "sensor.phone_battery", "state": "80",
                 "attributes": {"device_class": "battery", "friendly_name": "Phone"}}]


async def test_battery_status_skips_areas_while_reconnecting():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.get_battery_status",))
    defn = registry.get("get_battery_status")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=_BatteryRest(), ws=_HangingWS())
    result = await defn.handler(defn.params_model(), ctx)
    registry._reset_for_tests()
    assert result.status == "ok"


async def test_search_entities_skips_areas_while_reconnecting():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.search_entities",))
    defn = registry.get("search_entities")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=_BatteryRest(), ws=_HangingWS())
    result = await defn.handler(defn.params_model(query="phone"), ctx)
    registry._reset_for_tests()
    assert result.status == "ok"


async def test_needle_menu_skips_registry_while_reconnecting():
    from app.constants import AI_SCRIPT_PREFIX_ACTION
    from app.needle.menu import MenuProvider

    class Rest:
        async def list_states(self):
            return [{"entity_id": "automation.ai_action_night", "attributes": {}}]

    ws = _HangingWS()
    provider = MenuProvider(Rest(), prefixes=("automation.ai_action", AI_SCRIPT_PREFIX_ACTION), ws=ws)
    menu = await provider.get()
    assert len(menu.items) == 1
    assert ws.calls == []  # no 10s stall waiting on a reconnecting socket
