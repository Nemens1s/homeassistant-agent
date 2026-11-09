from app.agent.run_scope import RequestFacts
from app.config import Settings
from app.tools.context import ToolContext
from app.tools.helpers.request_context import (
    device_area,
    resolve_request_facts,
    user_person,
)


class FakeWS:
    def __init__(self, fail=False):
        self.fail = fail
        self.registries = {
            "config/area_registry/list": [
                {"area_id": "living_room", "name": "Living Room", "aliases": ["lounge"]},
                {"area_id": "bedroom", "name": "Bedroom"},
            ],
            "config/device_registry/list": [
                {"id": "abc", "area_id": "living_room"},
                {"id": "phone", "area_id": None},
                {"id": "ghost", "area_id": "deleted_area"},
            ],
        }

    async def request_cached(self, msg_type, ttl=60.0):
        if self.fail:
            raise RuntimeError("ws down")
        return self.registries[msg_type]


class FakeRest:
    def __init__(self, states=None, fail=False):
        self.states = states or []
        self.fail = fail

    async def list_states(self):
        if self.fail:
            raise RuntimeError("rest down")
        return self.states


_PEOPLE = [
    {"entity_id": "light.x", "attributes": {"user_id": "u1"}},
    {"entity_id": "person.guest", "attributes": {"friendly_name": "Guest"}},
    {"entity_id": "person.sofija", "attributes": {"user_id": "u2", "friendly_name": "Sofija"}},
    {"entity_id": "person.ilja", "attributes": {"user_id": "u1", "friendly_name": "Ilja"}},
]


def _ctx(ws=None, rest=None):
    settings = Settings(_env_file=None, ha_base_url="http://x", ha_token="t")
    return ToolContext(settings=settings, rest=rest or FakeRest(), ws=ws)


async def test_device_area_found():
    assert await device_area(_ctx(ws=FakeWS()), "abc") == ("living_room", "Living Room")


async def test_device_without_area_is_none():
    assert await device_area(_ctx(ws=FakeWS()), "phone") is None


async def test_device_area_without_registry_entry_is_none():
    assert await device_area(_ctx(ws=FakeWS()), "ghost") is None


async def test_unknown_device_is_none():
    assert await device_area(_ctx(ws=FakeWS()), "nope") is None


async def test_missing_device_id_is_none():
    assert await device_area(_ctx(ws=FakeWS()), None) is None


async def test_ws_none_is_none():
    assert await device_area(_ctx(ws=None), "abc") is None


async def test_ws_error_is_none():
    assert await device_area(_ctx(ws=FakeWS(fail=True)), "abc") is None


async def test_person_match():
    ctx = _ctx(rest=FakeRest(_PEOPLE))
    assert await user_person(ctx, "u1") == ("Ilja", "person.ilja")


async def test_person_no_match():
    states = [_PEOPLE[1], _PEOPLE[2]]
    assert await user_person(_ctx(rest=FakeRest(states)), "u1") is None


async def test_person_rest_error_is_none():
    assert await user_person(_ctx(rest=FakeRest(fail=True)), "u1") is None


async def test_resolve_request_facts_combines_and_tolerates_none_ids():
    ctx = _ctx(ws=FakeWS(), rest=FakeRest(_PEOPLE))
    assert (await resolve_request_facts(ctx, None, None)).is_empty
    facts = await resolve_request_facts(ctx, "abc", "u1")
    assert facts == RequestFacts("living_room", "Living Room", "Ilja", "person.ilja")


# --- room matching (fast path) ---

from app.tools.helpers.request_context import names_a_room, room_terms  # noqa: E402

_AREAS = [
    {"area_id": "living_room", "name": "Living Room", "aliases": ["lounge"]},
    {"area_id": "bedroom", "name": "Bedroom"},
    {"area_id": "hall", "name": "Hall"},
]


def test_names_a_room_by_name_id_alias_and_enum():
    terms = room_terms(_AREAS, ["living_room", "bedroom", "balcony"])
    assert names_a_room("dim the living room", terms)
    assert names_a_room("dim the lounge", terms)
    assert names_a_room("bedroom lights off", terms)
    assert names_a_room("lights on at the balcony", terms)
    assert not names_a_room("dim the lights to 15%", terms)


def test_names_a_room_tolerates_hyphen_and_possessive():
    terms = room_terms(_AREAS, [])
    assert names_a_room("living-room lights on", terms)
    assert names_a_room("the bedroom's lamp", terms)
    assert names_a_room("turn off the bedrooms", terms)


def test_names_a_room_word_bounded():
    terms = room_terms(_AREAS, [])
    assert not names_a_room("light in the hallway", terms)
