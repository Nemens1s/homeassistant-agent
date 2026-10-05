import pytest
from pydantic import ValidationError

from app.agent.run_scope import RunScope, use_scope
from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext

MODULES = (
    "app.tools.action.notify_user",
    "app.tools.action.trigger_action",
    "app.tools.read.list_actions",
)


class FakeRest:
    def __init__(self):
        self.calls = []

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, data))
        return []

    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    async def list_states(self):
        return [
            {"entity_id": "script.ai_action_notify", "state": "off", "attributes": {"friendly_name": "Notify"}},
            {"entity_id": "script.ai_action_stop_vacuum", "state": "off", "attributes": {"friendly_name": "Stop Vacuum"}},
        ]

    async def get_script_config(self, object_id):
        return {}


class FakeLang:
    async def from_english(self, text, language):
        if language == "en":
            return text
        return f"[{language}] {text}"

    async def to_english(self, text):
        return text, "en"


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(MODULES)
    yield
    registry._reset_for_tests()


def _ctx(rest, **overrides):
    settings = Settings(_env_file=None, **overrides)
    return ToolContext(settings=settings, rest=rest, ws=None, lang=FakeLang())


async def _call(name, ctx, **args):
    defn = registry.get(name)
    return await defn.handler(defn.params_model(**args), ctx)


async def test_sends_translated_message_via_notify_script():
    rest = FakeRest()
    with use_scope(RunScope(language="ru")):
        result = await _call("notify_user", _ctx(rest), message="Vacuum stopped.", title="Gosling")
    assert result.status == "ok"
    assert result.data == {"sent": True}
    assert rest.calls == [
        ("script", "ai_action_notify", {"message": "[ru] Vacuum stopped.", "title": "[ru] Gosling"})
    ]


async def test_suppressed_run_looks_identical_but_sends_nothing():
    delivered_rest = FakeRest()
    delivered = await _call("notify_user", _ctx(delivered_rest), message="Vacuum stopped.")
    rest = FakeRest()
    with use_scope(RunScope(suppress_notify=True)):
        suppressed = await _call("notify_user", _ctx(rest), message="Vacuum stopped.")
    assert suppressed.to_json() == delivered.to_json()
    assert rest.calls == []
    assert len(delivered_rest.calls) == 1


async def test_notify_user_is_action_tier():
    assert int(registry.get("notify_user").tier) == 2


async def test_rejects_non_ai_notify_action():
    rest = FakeRest()
    result = await _call("notify_user", _ctx(rest, notify_action="script.notify_all"), message="x")
    assert result.error_code == "not_configured"
    assert rest.calls == []


async def test_requires_script_domain():
    rest = FakeRest()
    result = await _call("notify_user", _ctx(rest, allowed_domains=["light"]), message="x")
    assert result.error_code == "domain_not_allowed"


def test_message_length_is_capped():
    with pytest.raises(ValidationError):
        registry.get("notify_user").params_model(message="x" * 501)


async def test_trigger_action_refuses_the_notify_script():
    rest = FakeRest()
    result = await _call("trigger_action", _ctx(rest), entity_id="script.ai_action_notify")
    assert result.error_code == "use_notify_user"
    assert rest.calls == []


async def test_list_actions_hides_the_notify_script():
    result = await _call("list_actions", _ctx(FakeRest()))
    ids = []
    for row in result.data["rows"]:
        ids.append(row["entity_id"])
    assert ids == ["script.ai_action_stop_vacuum"]
