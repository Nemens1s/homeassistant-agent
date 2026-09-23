from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    async def list_states(self):
        return [
            {"entity_id": "automation.ai_night_lights", "state": "on",
             "attributes": {"friendly_name": "AI: Night Lights"}},
            {"entity_id": "automation.morning", "state": "on",
             "attributes": {"friendly_name": "Morning"}},
            {"entity_id": "script.ai_action_lights_on", "state": "off",
             "attributes": {"friendly_name": "Lights On"}},
            {"entity_id": "script.backup", "state": "off", "attributes": {}},
            {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
        ]

    async def get_script_config(self, object_id):
        assert object_id == "ai_action_lights_on"
        return {"fields": {"room": {"required": True,
                                    "selector": {"select": {"options": ["living_room"]}}}}}


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=None)


async def test_list_actions_includes_ai_scripts_and_automations():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.list_actions",))
    defn = registry.get("list_actions")
    result = await defn.handler(defn.params_model(), _ctx())
    registry._reset_for_tests()
    ids = {r["entity_id"] for r in result.data["rows"]}
    assert ids == {"automation.ai_night_lights", "script.ai_action_lights_on"}


async def test_list_actions_script_row_carries_params():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.list_actions",))
    defn = registry.get("list_actions")
    result = await defn.handler(defn.params_model(), _ctx())
    registry._reset_for_tests()
    rows = {r["entity_id"]: r for r in result.data["rows"]}
    assert rows["script.ai_action_lights_on"]["params"]["properties"]["room"]["enum"] \
        == ["living_room"]
    assert "params" not in rows["automation.ai_night_lights"]
