import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    def __init__(self):
        self.calls = []
        self.state = {"entity_id": "light.kitchen", "state": "off",
                      "attributes": {"friendly_name": "Kitchen Light"},
                      "last_changed": "2026-07-13T10:00:00+00:00"}

    async def call_service(self, domain, service, entity_id):
        self.calls.append((domain, service, entity_id))
        return []

    async def get_state(self, entity_id):
        return dict(self.state, entity_id=entity_id)


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all((
        "app.tools.action.control_entity",
        "app.tools.action.trigger_automation",
    ))
    yield
    registry._reset_for_tests()


class FakeWS:
    """Minimal WS stub for label tests."""
    def __init__(self, entity_labels=None, device_labels=None):
        self._entity_labels = entity_labels or {}  # {entity_id: [label_ids]}
        self._device_labels = device_labels or {}  # {device_id: [label_ids]}

    async def request_cached(self, msg_type):
        if msg_type == "config/label_registry/list":
            return [{"label_id": "ai_allowed", "name": "AI Allowed"},
                    {"label_id": "critical", "name": "Critical"}]
        if msg_type == "config/entity_registry/list":
            return [{"entity_id": eid, "labels": lbls, "device_id": None}
                    for eid, lbls in self._entity_labels.items()]
        if msg_type == "config/device_registry/list":
            return [{"id": did, "labels": lbls}
                    for did, lbls in self._device_labels.items()]
        return []


def _ctx(rest=None, allowed=None, allowed_labels=None, ws=None):
    settings = Settings(_env_file=None)
    if allowed is not None:
        settings.allowed_domains = allowed
    if allowed_labels is not None:
        settings.allowed_labels = allowed_labels
    return ToolContext(settings=settings, rest=rest or FakeRest(), ws=ws)


async def test_control_entity_calls_service_and_confirms():
    rest = FakeRest()
    defn = registry.get("control_entity")
    assert int(defn.tier) == 2
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", action="turn_off"), _ctx(rest))
    assert result.status == "ok"
    assert rest.calls == [("light", "turn_off", "light.kitchen")]
    assert result.data["action"] == "turn_off"


async def test_control_entity_blocks_unavailable_entity():
    rest = FakeRest()
    rest.state = {"entity_id": "light.concorde", "state": "unavailable", "attributes": {}}
    defn = registry.get("control_entity")
    result = await defn.handler(
        defn.params_model(entity_id="light.concorde", action="turn_off"), _ctx(rest))
    assert result.status == "error"
    assert result.error_code == "entity_unavailable"
    assert rest.calls == []  # service never called


async def test_control_entity_denies_non_allowlisted_domain():
    rest = FakeRest()
    defn = registry.get("control_entity")
    result = await defn.handler(
        defn.params_model(entity_id="lock.front", action="turn_off"), _ctx(rest))
    assert result.status == "error"
    assert result.error_code == "domain_not_allowed"
    assert result.data["allowed"] == ["light", "switch", "fan", "automation"]
    assert rest.calls == []                        # never reached the client


async def test_control_entity_action_is_schema_constrained():
    defn = registry.get("control_entity")
    with pytest.raises(Exception):                 # pydantic ValidationError
        defn.params_model(entity_id="light.kitchen", action="explode")


async def test_trigger_automation_happy_path():
    rest = FakeRest()
    rest.state = {"entity_id": "automation.night", "state": "on",
                  "attributes": {"last_triggered": "2026-07-13T22:00:00+00:00"},
                  "last_changed": ""}
    defn = registry.get("trigger_automation")
    assert int(defn.tier) == 2
    result = await defn.handler(defn.params_model(entity_id="automation.night"), _ctx(rest))
    assert result.status == "ok"
    assert rest.calls == [("automation", "trigger", "automation.night")]
    assert result.data["last_triggered"] == "2026-07-13T22:00:00+00:00"


async def test_trigger_automation_rejects_non_automation_entity():
    defn = registry.get("trigger_automation")
    result = await defn.handler(defn.params_model(entity_id="light.kitchen"), _ctx())
    assert result.error_code == "invalid_params"


async def test_trigger_automation_respects_allowlist():
    defn = registry.get("trigger_automation")
    result = await defn.handler(
        defn.params_model(entity_id="automation.night"), _ctx(allowed=["light"]))
    assert result.error_code == "domain_not_allowed"


async def test_control_entity_label_check_passes_when_label_present():
    ws = FakeWS(entity_labels={"light.kitchen": ["ai_allowed"]})
    defn = registry.get("control_entity")
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", action="turn_off"),
        _ctx(allowed_labels=["AI Allowed"], ws=ws))
    assert result.status == "ok"


async def test_control_entity_label_check_blocks_when_label_absent():
    ws = FakeWS(entity_labels={"light.kitchen": []})
    defn = registry.get("control_entity")
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", action="turn_off"),
        _ctx(allowed_labels=["AI Allowed"], ws=ws))
    assert result.error_code == "label_not_allowed"


async def test_control_entity_label_check_skipped_when_ws_unavailable():
    # WS down → fail-open, action proceeds normally
    defn = registry.get("control_entity")
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", action="turn_off"),
        _ctx(allowed_labels=["AI Allowed"], ws=None))
    assert result.status == "ok"


async def test_control_entity_label_check_skipped_when_allowed_labels_empty():
    # allowed_labels=[] → feature disabled, no WS call needed
    ws = FakeWS(entity_labels={"light.kitchen": []})
    defn = registry.get("control_entity")
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", action="turn_off"),
        _ctx(allowed_labels=[], ws=ws))
    assert result.status == "ok"


async def test_trigger_automation_label_check_blocks():
    rest = FakeRest()
    rest.state = {"entity_id": "automation.night", "state": "on",
                  "attributes": {"last_triggered": ""}, "last_changed": ""}
    ws = FakeWS(entity_labels={"automation.night": []})
    defn = registry.get("trigger_automation")
    result = await defn.handler(
        defn.params_model(entity_id="automation.night"),
        _ctx(rest=rest, allowed_labels=["AI Allowed"], ws=ws))
    assert result.error_code == "label_not_allowed"
    assert rest.calls == []
