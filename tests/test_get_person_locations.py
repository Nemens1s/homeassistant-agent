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


class FakeRest:
    async def list_states(self):
        return STATES


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.get_person_locations",))
    yield
    registry._reset_for_tests()


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=None)


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
