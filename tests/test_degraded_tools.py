import pytest

from app.config import Settings
from app.degraded.tools import ToolRunner
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    def __init__(self, switch="on"):
        self.switch = switch
        self.calls = []

    async def get_state(self, entity_id):
        if entity_id == "input_boolean.ai_triggered_actions":
            return {"entity_id": entity_id, "state": self.switch, "attributes": {}}
        return {"entity_id": entity_id, "state": "off", "attributes": {}}

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id))
        return []


@pytest.fixture(autouse=True)
def tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.action.trigger_action",))
    yield
    registry._reset_for_tests()


def _runner(rest, enabled=None, allow_actions=True):
    settings = Settings(_env_file=None, allow_actions=allow_actions)
    ctx = ToolContext(settings=settings, rest=rest, ws=None)
    return ToolRunner(ctx, enabled, "t1")


async def test_run_goes_through_adapter_and_records_call():
    rest = FakeRest()
    runner = _runner(rest)
    env = await runner.run("trigger_action", {"entity_id": "automation.ai_goodnight", "params": {}})
    assert env["status"] == "ok"
    assert rest.calls == [("automation", "trigger", "automation.ai_goodnight")]
    assert runner.calls == [{"name": "trigger_action", "status": "ok"}]


async def test_gate_applies():
    rest = FakeRest(switch="off")
    env = await _runner(rest).run("trigger_action", {"entity_id": "automation.ai_goodnight", "params": {}})
    assert env["error"]["code"] == "ai_disabled"
    assert rest.calls == []


def test_has_respects_enabled_set_and_tier():
    assert _runner(FakeRest(), enabled={"trigger_action"}).has("trigger_action") is True
    assert _runner(FakeRest(), enabled=set()).has("trigger_action") is False
    assert _runner(FakeRest(), enabled=None, allow_actions=False).has("trigger_action") is False
    assert _runner(FakeRest()).has("no_such_tool") is False
