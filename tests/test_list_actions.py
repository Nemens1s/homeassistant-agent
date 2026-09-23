from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    async def list_states(self):
        return [
            {"entity_id": "automation.ai_night_lights", "state": "on",
             "attributes": {"friendly_name": "AI: Night Lights"}},
            {"entity_id": "automation.ai_morning_brew", "state": "off",
             "attributes": {"friendly_name": "AI: Morning Brew"}},
            {"entity_id": "automation.morning", "state": "on",
             "attributes": {"friendly_name": "Morning"}},
            {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
        ]


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=None)


async def test_list_actions_lists_only_ai_controllable():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.list_actions",))
    defn = registry.get("list_actions")
    result = await defn.handler(defn.params_model(), _ctx())
    registry._reset_for_tests()
    assert result.status == "ok"
    ids = {r["entity_id"] for r in result.data["rows"]}
    assert ids == {"automation.ai_night_lights", "automation.ai_morning_brew"}


async def test_list_actions_rows_carry_name_and_state():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.list_actions",))
    defn = registry.get("list_actions")
    result = await defn.handler(defn.params_model(), _ctx())
    registry._reset_for_tests()
    rows = {r["entity_id"]: r for r in result.data["rows"]}
    row = rows["automation.ai_night_lights"]
    assert row["name"] == "AI: Night Lights"
    assert row["state"] == "on"
    # The domain lives in the entity_id prefix; no redundant "type" field.
    assert "type" not in row
