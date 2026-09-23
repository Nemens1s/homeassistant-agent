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
            {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
        ]


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=None)


async def test_get_automations_lists_all_automations():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.get_automations",))
    defn = registry.get("get_automations")
    result = await defn.handler(defn.params_model(entity_id=""), _ctx())
    registry._reset_for_tests()
    assert result.status == "ok"
    rows = {r["entity_id"]: r for r in result.data["rows"]}
    # Every automation is listed, AI-controllable or not...
    assert set(rows) == {"automation.ai_night_lights", "automation.morning"}
    assert "light.kitchen" not in rows  # non-automations excluded
    # ...and the ai_controllable concept now lives solely in list_actions.
    assert "ai_controllable" not in rows["automation.ai_night_lights"]
