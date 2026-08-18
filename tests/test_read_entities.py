import pytest
import httpx

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
    # diagnostic entity — should be excluded by device filter
    {"entity_id": "sensor.kitchen_power", "state": "150",
     "attributes": {"friendly_name": "Kitchen Power"},
     "last_changed": "2026-07-13T10:00:00+00:00"},
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
            "config/device_registry/list": [
                {"id": "dev1", "area_id": "kitchen", "name_by_user": None, "name": "Hue Bulb"},
                {"id": "dev2", "area_id": None, "name_by_user": None, "name": "Smart Plug"},
            ],
            "config/entity_registry/list": [
                {"entity_id": "light.kitchen", "area_id": None, "device_id": "dev1", "entity_category": None},
                {"entity_id": "sensor.kitchen_power", "area_id": None, "device_id": "dev1", "entity_category": "diagnostic"},
                {"entity_id": "light.bedroom", "area_id": None, "device_id": None, "entity_category": None},
                {"entity_id": "switch.plug", "area_id": None, "device_id": "dev2", "entity_category": None},
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
    assert result.data["entity_id"] == "light.kitchen"
    assert result.data["last_changed"] == "2026-07-13T10:00:00+00:00"


async def test_list_entities_domain_filter():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(domain="light"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen", "light.bedroom"]
    assert result.data["total"] == 2


async def test_list_entities_state_filter():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(state="on"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen"]
    assert "light.bedroom" not in ids


async def test_list_entities_state_and_domain_filter():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(domain="light", state="off"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.bedroom"]


async def test_list_entities_device_filter():
    """device filter returns only non-diagnostic entities for the matched device."""
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(device="Hue Bulb"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert "light.kitchen" in ids
    # diagnostic entity excluded
    assert "sensor.kitchen_power" not in ids


async def test_list_entities_device_substring_match():
    """device is matched by case-insensitive substring."""
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(device="hue"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    assert result.data["rows"][0]["entity_id"] == "light.kitchen"


async def test_list_entities_device_not_found():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(device="Nonexistent"), _ctx(ws=FakeWS()))
    assert result.status == "error"
    assert result.error_code == "device_not_found"
    assert "did_you_mean" in result.data


async def test_list_entities_device_without_ws():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(device="Hue Bulb"), _ctx(ws=None))
    assert result.status == "error"
    assert result.error_code == "ws_unavailable"


async def test_list_entities_device_and_domain_combined():
    """domain filter still applies on top of device filter."""
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(device="Hue Bulb", domain="sensor"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    # light.kitchen is filtered out by domain=sensor; diagnostic sensor also excluded
    assert result.data["rows"] == []


async def test_get_entity_state_propagates_http_error():
    """404 from REST client is not caught and propagates."""
    class Raising404Rest:
        async def get_state(self, entity_id):
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404),
            )

    defn = registry.get("get_entity_state")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=Raising404Rest(), ws=None)
    with pytest.raises(httpx.HTTPStatusError):
        await defn.handler(defn.params_model(entity_id="light.nope"), ctx)


async def test_list_entities_rejects_unknown_domain_enum():
    """domain is a constrained enum — an invented value fails validation."""
    defn = registry.get("list_entities")
    with pytest.raises(Exception):  # pydantic ValidationError
        defn.params_model(domain="not_a_real_domain")


async def test_list_entities_domain_enum_exposed_in_schema():
    """The LLM must see the allowed domains as a JSON-schema enum, not a free string."""
    defn = registry.get("list_entities")
    schema = defn.params_model.model_json_schema()
    domain_field = schema["properties"]["domain"]
    # pydantic renders Literal as an enum (possibly via allOf/$ref); flatten to check.
    dumped = str(domain_field) + str(schema.get("$defs", ""))
    assert "light" in dumped and "climate" in dumped
