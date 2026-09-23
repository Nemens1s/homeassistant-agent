import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    def __init__(self):
        self.calls = []
        self.state = {"entity_id": "automation.ai_night", "state": "on",
                      "attributes": {"last_triggered": "2026-07-13T22:00:00+00:00"},
                      "last_changed": ""}

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id, data))
        return []

    async def get_state(self, entity_id):
        return dict(self.state, entity_id=entity_id)

    async def get_script_config(self, object_id):
        return {"fields": {"room": {"required": True,
                                    "selector": {"select": {"options": ["living_room"]}}}}}


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.action.trigger_action",))
    yield
    registry._reset_for_tests()


def _ctx(rest=None, allowed=None):
    settings = Settings(_env_file=None)
    if allowed is not None:
        settings.allowed_domains = allowed
    return ToolContext(settings=settings, rest=rest or FakeRest(), ws=None)


async def test_trigger_action_happy_path():
    rest = FakeRest()
    defn = registry.get("trigger_action")
    assert int(defn.tier) == 2
    result = await defn.handler(defn.params_model(entity_id="automation.ai_night"), _ctx(rest))
    assert result.status == "ok"
    assert rest.calls == [("automation", "trigger", "automation.ai_night", None)]
    assert result.data["last_triggered"] == "2026-07-13T22:00:00+00:00"


async def test_trigger_action_rejects_non_automation_entity():
    defn = registry.get("trigger_action")
    result = await defn.handler(defn.params_model(entity_id="light.kitchen"), _ctx())
    assert result.error_code == "invalid_params"


async def test_trigger_action_rejects_non_ai_automation():
    rest = FakeRest()
    defn = registry.get("trigger_action")
    result = await defn.handler(defn.params_model(entity_id="automation.night"), _ctx(rest))
    assert result.error_code == "not_ai_controllable"
    assert rest.calls == []


async def test_trigger_action_respects_allowlist():
    defn = registry.get("trigger_action")
    result = await defn.handler(
        defn.params_model(entity_id="automation.ai_night"), _ctx(allowed=["light"]))
    assert result.error_code == "domain_not_allowed"


async def test_trigger_action_runs_script_with_params():
    rest = FakeRest()
    defn = registry.get("trigger_action")
    result = await defn.handler(
        defn.params_model(entity_id="script.ai_action_lights_on",
                          params={"room": "living_room"}),
        _ctx(rest, allowed=["script"]))
    assert result.status == "ok"
    assert rest.calls == [("script", "ai_action_lights_on", None, {"room": "living_room"})]


async def test_trigger_action_rejects_unknown_param_key():
    rest = FakeRest()
    defn = registry.get("trigger_action")
    result = await defn.handler(
        defn.params_model(entity_id="script.ai_action_lights_on",
                          params={"zone": "living_room"}),
        _ctx(rest, allowed=["script"]))
    assert result.error_code == "invalid_params"
    assert rest.calls == []


async def test_trigger_action_rejects_missing_required_param():
    rest = FakeRest()
    defn = registry.get("trigger_action")
    result = await defn.handler(
        defn.params_model(entity_id="script.ai_action_lights_on", params={}),
        _ctx(rest, allowed=["script"]))
    assert result.error_code == "invalid_params"
    assert rest.calls == []


async def test_trigger_action_rejects_non_ai_script():
    rest = FakeRest()
    defn = registry.get("trigger_action")
    result = await defn.handler(
        defn.params_model(entity_id="script.backup"), _ctx(rest, allowed=["script"]))
    assert result.error_code == "not_ai_controllable"
    assert rest.calls == []
