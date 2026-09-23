from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX_ACTION
from app.needle.menu import MenuProvider, fields_to_parameters


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
    provider = MenuProvider(FakeRest(STATES), ttl_s=60)
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
