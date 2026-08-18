import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {"entity_id": "person.ilja", "state": "home",
     "attributes": {"friendly_name": "Ilja"}},
    {"entity_id": "person.sofija", "state": "not_home",
     "attributes": {"friendly_name": "Sofija"}},
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen Light"}},
]

STATES_WITH_ACCOUNT_NAMES = [
    {"entity_id": "person.ilja", "state": "home",
     "attributes": {"friendly_name": "ilniko"}},
    {"entity_id": "person.sofija", "state": "not_home",
     "attributes": {"friendly_name": "megakrasotka2002"}},
    {"entity_id": "person.ipad", "state": "home",
     "attributes": {"friendly_name": "Ilja's iPad"}},
]


class FakeRest:
    def __init__(self, states=None):
        self._states = states if states is not None else STATES

    async def list_states(self):
        return self._states


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.get_person_locations",))
    yield
    registry._reset_for_tests()


def _ctx(states=None, name_map=None, exclude=None):
    settings = Settings(
        _env_file=None,
        person_name_map=name_map or [],
        person_name_exclude=exclude or [],
    )
    return ToolContext(settings=settings, rest=FakeRest(states), ws=None)


def _defn():
    return registry.get("get_person_locations")


async def test_returns_only_person_entities():
    result = await _defn().handler(_defn().params_model(), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert "person.ilja" in ids
    assert "person.sofija" in ids
    assert "light.kitchen" not in ids


async def test_includes_state_and_name():
    result = await _defn().handler(_defn().params_model(), _ctx())
    assert result.status == "ok"
    ilja = next(r for r in result.data["rows"] if r["entity_id"] == "person.ilja")
    assert ilja["state"] == "home"
    assert ilja["name"] == "Ilja"


async def test_not_home_state_preserved():
    result = await _defn().handler(_defn().params_model(), _ctx())
    assert result.status == "ok"
    sofija = next(r for r in result.data["rows"] if r["entity_id"] == "person.sofija")
    assert sofija["state"] == "not_home"


async def test_returns_empty_when_no_persons():
    class NoPersonsRest:
        async def list_states(self):
            return [{"entity_id": "light.kitchen", "state": "on", "attributes": {}}]

    ctx = ToolContext(settings=Settings(_env_file=None), rest=NoPersonsRest(), ws=None)
    result = await _defn().handler(_defn().params_model(), ctx)
    assert result.status == "ok"
    assert result.data["rows"] == []


async def test_name_map_rewrites_account_names():
    name_map = [
        {"ha_name": "ilniko", "name": "Ilja"},
        {"ha_name": "megakrasotka2002", "name": "Sofija"},
    ]
    result = await _defn().handler(
        _defn().params_model(),
        _ctx(states=STATES_WITH_ACCOUNT_NAMES, name_map=name_map),
    )
    assert result.status == "ok"
    rows = {r["entity_id"]: r["name"] for r in result.data["rows"]}
    assert rows["person.ilja"] == "Ilja"
    assert rows["person.sofija"] == "Sofija"


async def test_aliases_appear_in_output():
    """Alias entries (same canonical name, different ha_name) surface as 'aliases'
    so the agent can resolve 'Is Sonja home?' when the entity shows 'Sofija'."""
    name_map = [
        {"ha_name": "megakrasotka2002", "name": "Sofija"},
        {"ha_name": "Sonja", "name": "Sofija"},
        {"ha_name": "Sonya", "name": "Sofija"},
    ]
    result = await _defn().handler(
        _defn().params_model(),
        _ctx(states=STATES_WITH_ACCOUNT_NAMES, name_map=name_map),
    )
    assert result.status == "ok"
    sofija = next(r for r in result.data["rows"] if r["entity_id"] == "person.sofija")
    assert sofija["name"] == "Sofija"
    assert set(sofija["aliases"]) == {"Sonja", "Sonya"}


async def test_no_aliases_key_when_no_aliases():
    name_map = [
        {"ha_name": "ilniko", "name": "Ilja"},
        {"ha_name": "megakrasotka2002", "name": "Sofija"},
    ]
    result = await _defn().handler(
        _defn().params_model(),
        _ctx(states=STATES_WITH_ACCOUNT_NAMES, name_map=name_map),
    )
    assert result.status == "ok"
    for row in result.data["rows"]:
        assert "aliases" not in row


async def test_exclude_hides_matching_persons():
    result = await _defn().handler(
        _defn().params_model(),
        _ctx(states=STATES_WITH_ACCOUNT_NAMES, exclude=["ipad"]),
    )
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert "person.ipad" not in ids
    assert "person.ilja" in ids


async def test_exclude_is_case_insensitive():
    result = await _defn().handler(
        _defn().params_model(),
        _ctx(states=STATES_WITH_ACCOUNT_NAMES, exclude=["iPad"]),
    )
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert "person.ipad" not in ids


async def test_no_map_no_exclude_passes_through():
    result = await _defn().handler(_defn().params_model(), _ctx())
    assert result.status == "ok"
    assert len(result.data["rows"]) == 2
