import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {
        "entity_id": "vacuum.roborock_qrevo_s",
        "state": "docked",
        "attributes": {"friendly_name": "Roborock Qrevo S"},
    },
    {
        "entity_id": "sensor.roborock_battery",
        "state": "85",
        "attributes": {"friendly_name": "Roborock Battery"},
    },
    {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen Light"},
    },
]


class FakeRest:
    async def list_states(self):
        return STATES


class FakeWS:
    async def request_cached(self, msg_type, ttl=60.0):
        if msg_type == "config/area_registry/list":
            return [{"area_id": "living_room", "name": "Living Room"}]
        if msg_type == "config/device_registry/list":
            return [{"id": "dev_vacuum", "area_id": "living_room"}]
        if msg_type == "config/entity_registry/list":
            return [
                {"entity_id": "vacuum.roborock_qrevo_s", "area_id": None, "device_id": "dev_vacuum"},
                {"entity_id": "sensor.roborock_battery", "area_id": None, "device_id": None},
                {"entity_id": "light.kitchen", "area_id": None, "device_id": None},
            ]
        return []


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.search_entities",))
    yield
    registry._reset_for_tests()


def _ctx(ws=None):
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=ws)


def _defn():
    return registry.get("search_entities")


async def test_search_matches_friendly_name():
    result = await _defn().handler(_defn().params_model(query="roborock"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert "vacuum.roborock_qrevo_s" in ids
    assert "sensor.roborock_battery" in ids
    assert "light.kitchen" not in ids


async def test_search_matches_entity_id():
    result = await _defn().handler(_defn().params_model(query="kitchen"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen"]


async def test_search_case_insensitive():
    result = await _defn().handler(_defn().params_model(query="ROBOROCK"), _ctx())
    assert result.status == "ok"
    assert len(result.data["rows"]) == 2


async def test_search_multi_word_query_matches_any_word():
    # "roborock cleaner" — "cleaner" matches nothing, "roborock" matches two entities
    result = await _defn().handler(_defn().params_model(query="roborock cleaner"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert "vacuum.roborock_qrevo_s" in ids
    assert "sensor.roborock_battery" in ids


async def test_search_no_match_returns_empty():
    result = await _defn().handler(_defn().params_model(query="nonexistent"), _ctx())
    assert result.status == "ok"
    assert result.data["rows"] == []


async def test_search_includes_state():
    result = await _defn().handler(_defn().params_model(query="roborock qrevo"), _ctx())
    assert result.status == "ok"
    row = result.data["rows"][0]
    assert row["state"] == "docked"
    assert row["friendly_name"] == "Roborock Qrevo S"


async def test_search_includes_area_when_ws_available():
    result = await _defn().handler(_defn().params_model(query="roborock qrevo"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    row = result.data["rows"][0]
    assert row["area"] == "Living Room"


async def test_search_omits_area_without_ws():
    result = await _defn().handler(_defn().params_model(query="roborock qrevo"), _ctx(ws=None))
    assert result.status == "ok"
    row = result.data["rows"][0]
    assert "area" not in row


RANKING_STATES = [
    {"entity_id": "weather.home", "state": "sunny", "attributes": {"friendly_name": "Weather"}},
    {"entity_id": "climate.thermostat", "state": "heat", "attributes": {"friendly_name": "The Thermostat"}},
    {"entity_id": "sensor.kitchen_temperature", "state": "21", "attributes": {"friendly_name": "Kitchen Temperature"}},
    {"entity_id": "light.kitchen_ceiling", "state": "on", "attributes": {"friendly_name": "Kitchen Ceiling Light"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light"}},
    {"entity_id": "light.hall", "state": "off", "attributes": {"friendly_name": "Hall Light"}},
]


class RankingRest:
    async def list_states(self):
        return RANKING_STATES


def _ranking_ctx(**overrides):
    return ToolContext(settings=Settings(_env_file=None, **overrides), rest=RankingRest(), ws=None)


async def test_search_drops_stop_words_and_prefers_all_word_matches():
    result = await _defn().handler(_defn().params_model(query="the kitchen light"), _ranking_ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids[:2] == ["light.kitchen_ceiling", "light.kitchen"]
    assert "weather.home" not in ids
    assert "climate.thermostat" not in ids


async def test_search_ranks_partial_matches_below_full_ones():
    result = await _defn().handler(_defn().params_model(query="kitchen lamp"), _ranking_ctx())
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen_ceiling", "light.kitchen", "sensor.kitchen_temperature"]


async def test_search_strips_punctuation():
    result = await _defn().handler(_defn().params_model(query="hall light?"), _ranking_ctx())
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids[0] == "light.hall"


async def test_search_exact_entity_id_returns_one_row():
    result = await _defn().handler(_defn().params_model(query="light.kitchen"), _ranking_ctx())
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen"]


async def test_search_only_stop_words_is_invalid_params():
    result = await _defn().handler(_defn().params_model(query="the of my?"), _ranking_ctx())
    assert result.status == "error"
    assert result.error_code == "invalid_params"


async def test_search_caps_rows_at_twenty_by_default():
    many = []
    for i in range(30):
        many.append({"entity_id": f"sensor.probe_{i}", "state": "1", "attributes": {"friendly_name": f"Probe {i}"}})

    class ManyRest:
        async def list_states(self):
            return many

    ctx = ToolContext(settings=Settings(_env_file=None), rest=ManyRest(), ws=None)
    result = await _defn().handler(_defn().params_model(query="probe"), ctx)
    assert len(result.data["rows"]) == 20
    assert result.data["total"] == 30
    assert result.data["truncated"] is True
    assert result.data["hint"] == "Use more specific words to see the rest."


async def test_search_max_rows_stays_the_upper_bound():
    result = await _defn().handler(_defn().params_model(query="light"), _ranking_ctx(max_rows=1))
    assert len(result.data["rows"]) == 1


async def test_search_plural_still_finds_singular_names():
    # A "Kitchen Lights" group must not hide the individual kitchen lights.
    states = RANKING_STATES + [
        {"entity_id": "light.kitchen_lights", "state": "on", "attributes": {"friendly_name": "Kitchen Lights"}},
    ]

    class Rest:
        async def list_states(self):
            return states

    ctx = ToolContext(settings=Settings(_env_file=None), rest=Rest(), ws=None)
    result = await _defn().handler(_defn().params_model(query="kitchen lights"), ctx)
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids[:3] == ["light.kitchen_ceiling", "light.kitchen", "light.kitchen_lights"]


async def test_search_filler_words_are_not_required():
    result = await _defn().handler(_defn().params_model(query="all lights"), _ranking_ctx())
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert set(ids) == {"light.kitchen_ceiling", "light.kitchen", "light.hall"}


async def test_search_possessive_drops_one_letter_leftover():
    states = [
        {"entity_id": "light.mari_lamp", "state": "on", "attributes": {"friendly_name": "Mari Lamp"}},
        {"entity_id": "switch.mari_lamp_plug", "state": "on", "attributes": {"friendly_name": "Mari Lamp Plug"}},
        {"entity_id": "sensor.solar", "state": "1", "attributes": {"friendly_name": "Solar"}},
    ]

    class Rest:
        async def list_states(self):
            return states

    ctx = ToolContext(settings=Settings(_env_file=None), rest=Rest(), ws=None)
    result = await _defn().handler(_defn().params_model(query="Mari's lamp"), ctx)
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.mari_lamp", "switch.mari_lamp_plug"]


async def test_search_tolerates_null_friendly_name():
    states = [
        {"entity_id": "sensor.nameless", "state": "1", "attributes": {"friendly_name": None}},
        {"entity_id": "light.hall", "state": "off", "attributes": {"friendly_name": "Hall Light"}},
    ]

    class Rest:
        async def list_states(self):
            return states

    ctx = ToolContext(settings=Settings(_env_file=None), rest=Rest(), ws=None)
    result = await _defn().handler(_defn().params_model(query="nameless"), ctx)
    assert result.status == "ok"
    assert result.data["rows"][0]["friendly_name"] == ""
