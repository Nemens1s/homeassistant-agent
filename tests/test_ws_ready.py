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
