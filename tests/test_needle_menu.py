from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX_ACTION
import pytest

from app.needle.menu import MenuProvider, exact_match_opt_in, fields_to_parameters, needle_description


class FakeRest:
    def __init__(self, states, script_configs=None):
        self._states = states
        self._script_configs = script_configs or {}
        self.calls = 0

    async def list_states(self):
        self.calls += 1
        return self._states

    async def get_script_config(self, object_id):
        return self._script_configs[object_id]

    async def get_automation_config(self, unique_id):
        return {"description": f"NEEDLE: run {unique_id}."}


class FakeRegistryWS:
    """Entity registry: automation entity_id → unique_id (how descriptions are found)."""

    async def request_cached(self, msg_type, ttl=60.0):
        return [
            {"entity_id": "automation.ai_goodnight", "unique_id": "goodnight"},
            {"entity_id": "automation.ai_movie", "unique_id": "movie"},
        ]


STATES = [
    {"entity_id": "light.kitchen", "attributes": {}},
    {"entity_id": "automation.ai_goodnight", "attributes": {"friendly_name": "Goodnight"}},
    {"entity_id": "automation.morning", "attributes": {"friendly_name": "Morning"}},
    {"entity_id": "automation.ai_movie", "attributes": {"friendly_name": "Movie time"}},
]

SCRIPT_STATES = [
    {"entity_id": "script.ai_action_lights_on", "attributes": {"friendly_name": "Lights On"}},
]
SCRIPT_CONFIGS = {
    "ai_action_lights_on": {
        "alias": "Lights On",
        "description": "NEEDLE: turn on room lights.",
        "fields": {
            "room": {"description": "Which room", "required": True,
                     "selector": {"select": {"options": ["living_room"]}}},
        },
    },
}


def test_fields_to_parameters_maps_select_and_number():
    fields = {
        "room": {"description": "Which room", "required": True,
                 "selector": {"select": {"options": ["living_room", "kitchen"]}}},
        "percentage": {"description": "Brightness",
                       "selector": {"number": {"min": 1, "max": 100}}},
    }
    params = fields_to_parameters(fields)
    assert params == {
        "type": "object",
        "properties": {
            "room": {"type": "string", "enum": ["living_room", "kitchen"],
                     "description": "Which room"},
            "percentage": {"type": "integer", "minimum": 1, "maximum": 100,
                           "description": "Brightness"},
        },
        "required": ["room"],
    }


def test_fields_to_parameters_empty():
    assert fields_to_parameters({}) == {"type": "object", "properties": {}}


async def test_menu_keeps_only_ai_automations_with_names():
    provider = MenuProvider(FakeRest(STATES), ttl_s=60, ws=FakeRegistryWS())
    menu = await provider.get()
    ids = [item.entity_id for item in menu.items]
    assert ids == ["automation.ai_goodnight", "automation.ai_movie"]
    assert menu.items[0].name == "Goodnight"
    assert menu.items[0].parameters == {}


async def test_menu_includes_scripts_with_parameters():
    rest = FakeRest(SCRIPT_STATES, SCRIPT_CONFIGS)
    provider = MenuProvider(rest, ttl_s=60, prefixes=(AI_SCRIPT_PREFIX_ACTION,))
    menu = await provider.get()
    item = menu.items[0]
    assert item.entity_id == "script.ai_action_lights_on"
    assert item.parameters["properties"]["room"]["enum"] == ["living_room"]
    assert item.description == "turn on room lights."  # NEEDLE-stripped


async def test_signature_changes_when_parameters_change():
    cfg_a = {"ai_action_lights_on": dict(SCRIPT_CONFIGS["ai_action_lights_on"])}
    cfg_b = {"ai_action_lights_on": {
        **SCRIPT_CONFIGS["ai_action_lights_on"],
        "fields": {"room": {"required": True,
                            "selector": {"select": {"options": ["living_room", "kitchen"]}}}},
    }}
    a = await MenuProvider(FakeRest(SCRIPT_STATES, cfg_a), ttl_s=60,
                           prefixes=(AI_SCRIPT_PREFIX_ACTION,)).get()
    b = await MenuProvider(FakeRest(SCRIPT_STATES, cfg_b), ttl_s=60,
                           prefixes=(AI_SCRIPT_PREFIX_ACTION,)).get()
    assert a.signature != b.signature


async def test_menu_excludes_listed_entities():
    states = [
        {"entity_id": "script.ai_action_notify", "attributes": {}},
        {"entity_id": "script.ai_action_stop_vacuum", "attributes": {}},
    ]
    rest = FakeRest(states, {
        "ai_action_notify": {"description": "NEEDLE: notify."},  # opted in, still excluded
        "ai_action_stop_vacuum": {"description": "NEEDLE: stop the vacuum."},
    })
    provider = MenuProvider(
        rest, prefixes=(AI_SCRIPT_PREFIX_ACTION,), exclude=("script.ai_action_notify",)
    )
    menu = await provider.get()
    ids = []
    for item in menu.items:
        ids.append(item.entity_id)
    assert ids == ["script.ai_action_stop_vacuum"]


async def test_menu_keeps_only_opted_in_actions():
    states = [
        {"entity_id": "script.ai_action_lights_on", "attributes": {}},
        {"entity_id": "script.ai_action_fast", "attributes": {}},
        {"entity_id": "script.ai_action_notify", "attributes": {}},
    ]
    configs = {
        "ai_action_lights_on": {"description": "Turns on lights. NEEDLE: turn on the lights."},
        "ai_action_fast": {"description": "FAST-PATH: start the coffee machine."},
        "ai_action_notify": {"description": "Sends a push notification."},  # no marker
    }
    provider = MenuProvider(FakeRest(states, configs), prefixes=(AI_SCRIPT_PREFIX_ACTION,))
    menu = await provider.get()
    descriptions = {}
    for item in menu.items:
        descriptions[item.entity_id] = item.description
    assert descriptions == {
        "script.ai_action_lights_on": "turn on the lights.",
        "script.ai_action_fast": "start the coffee machine.",
    }


async def test_menu_picks_up_changed_script_fields_after_ttl():
    configs = {"ai_action_lights_on": SCRIPT_CONFIGS["ai_action_lights_on"]}
    provider = MenuProvider(FakeRest(SCRIPT_STATES, configs), ttl_s=0,
                            prefixes=(AI_SCRIPT_PREFIX_ACTION,))
    await provider.get()
    configs["ai_action_lights_on"] = {
        **SCRIPT_CONFIGS["ai_action_lights_on"],
        "fields": {"room": {"required": True,
                            "selector": {"select": {"options": ["living_room", "all"]}}}},
    }
    menu = await provider.get()
    assert menu.items[0].parameters["properties"]["room"]["enum"] == ["living_room", "all"]


class FlakyRest(FakeRest):
    """Script config reads fail after the first refresh."""

    def __init__(self, states, script_configs):
        super().__init__(states, script_configs)
        self.fail = False

    async def get_script_config(self, object_id):
        if self.fail:
            raise RuntimeError("HA unavailable")
        return await super().get_script_config(object_id)


async def test_menu_keeps_last_known_config_when_a_read_fails():
    rest = FlakyRest(SCRIPT_STATES, SCRIPT_CONFIGS)
    provider = MenuProvider(rest, ttl_s=0, prefixes=(AI_SCRIPT_PREFIX_ACTION,))
    first = await provider.get()
    rest.fail = True
    second = await provider.get()
    assert second.items == first.items


@pytest.mark.parametrize("description, expected", [
    ("Stops the vacuum. EXACT-MATCH. FAST-PATH: stop the vacuum", True),
    ("EXACT-MATCH", True),
    ("FAST-PATH: stop the vacuum", False),
    ("needs an exact-match to fire", False),
    ("NOT-EXACT-MATCH", False),
    ("EXACT-MATCHING", False),
])
def test_exact_match_opt_in(description, expected):
    assert exact_match_opt_in(description) is expected


def test_needle_description_drops_the_exact_match_flag():
    assert needle_description("FAST-PATH: stop the vacuum EXACT-MATCH") == "stop the vacuum"
    assert needle_description("Stops it. EXACT-MATCH. FAST-PATH: stop the vacuum") == "stop the vacuum"


async def test_exact_match_menu_keeps_flagged_actions_and_skips_off_automations():
    states = [
        {"entity_id": "script.ai_action_stop_vacuum", "state": "off",
         "attributes": {"friendly_name": "AI Action: Stop Vacuum"}},
        {"entity_id": "script.ai_action_garage_open", "state": "off",
         "attributes": {"friendly_name": "AI Action: Garage Door Open"}},
        {"entity_id": "script.ai_action_lights_on", "state": "off",
         "attributes": {"friendly_name": "AI Action: Lights On"}},
    ]
    configs = {
        "ai_action_stop_vacuum": {"description": "Stops it. EXACT-MATCH. FAST-PATH: stop the vacuum"},
        "ai_action_garage_open": {"description": "FAST-PATH: open the garage"},
        "ai_action_lights_on": {"description": "EXACT-MATCH"},
    }
    provider = MenuProvider(FakeRest(states, configs), prefixes=("script.ai_",),
                            opt_in=exact_match_opt_in, skip_disabled=True)
    menu = await provider.get()
    ids = []
    for item in menu.items:
        ids.append(item.entity_id)
    # Scripts are "off" while idle: skip_disabled only drops automations.
    assert ids == ["script.ai_action_lights_on", "script.ai_action_stop_vacuum"]


async def test_menu_items_carry_the_exact_match_flag():
    states = [
        {"entity_id": "script.ai_action_stop_vacuum", "state": "off",
         "attributes": {"friendly_name": "AI Action: Stop Vacuum"}},
        {"entity_id": "script.ai_action_garage_open", "state": "off",
         "attributes": {"friendly_name": "AI Action: Garage Door Open"}},
    ]
    configs = {
        "ai_action_stop_vacuum": {"description": "EXACT-MATCH. FAST-PATH: stop the vacuum"},
        "ai_action_garage_open": {"description": "FAST-PATH: open the garage"},
    }
    menu = await MenuProvider(FakeRest(states, configs), prefixes=("script.ai_",)).get()
    flags = {}
    for item in menu.items:
        flags[item.entity_id] = item.exact_match
    assert flags == {"script.ai_action_garage_open": False, "script.ai_action_stop_vacuum": True}


async def test_skip_disabled_drops_automations_that_are_off():
    states = [
        {"entity_id": "automation.ai_night", "state": "off", "attributes": {"friendly_name": "Night"}},
        {"entity_id": "automation.ai_movie", "state": "on", "attributes": {"friendly_name": "Movie"}},
    ]
    provider = MenuProvider(FakeRest(states), ws=FakeRegistryWS(), opt_in=lambda d: True,
                            skip_disabled=True)
    menu = await provider.get()
    assert [item.entity_id for item in menu.items] == ["automation.ai_movie"]
