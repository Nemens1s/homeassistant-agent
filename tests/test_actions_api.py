"""Tests for GET /api/actions and POST /api/actions/{entity_id}/toggle."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _settings():
    return Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
    )


ENTITY_REGISTRY = [
    {
        "entity_id": "automation.ai_night_lights",
        "name": None,
        "original_name": "AI Night Lights",
        "disabled_by": None,
    },
    {
        "entity_id": "automation.ai_away_mode",
        "name": None,
        "original_name": "AI Away Mode",
        "disabled_by": None,
    },
    {
        "entity_id": "script.ai_action_lights_on",
        "name": None,
        "original_name": "AI Action: Lights On",
        "disabled_by": None,
    },
    {
        "entity_id": "script.ai_action_music",
        "name": None,
        "original_name": "AI Action: Music",
        "disabled_by": "user",
    },
    # non-AI entities should be excluded
    {
        "entity_id": "automation.morning_routine",
        "name": None,
        "original_name": "Morning Routine",
        "disabled_by": None,
    },
    {
        "entity_id": "light.kitchen",
        "name": None,
        "original_name": "Kitchen",
        "disabled_by": None,
    },
]

STATES = [
    {
        "entity_id": "automation.ai_night_lights",
        "state": "on",
        "attributes": {"friendly_name": "AI Night Lights"},
    },
    {
        "entity_id": "automation.ai_away_mode",
        "state": "off",
        "attributes": {"friendly_name": "AI Away Mode"},
    },
    {
        "entity_id": "script.ai_action_lights_on",
        "state": "off",
        "attributes": {"friendly_name": "AI Action: Lights On"},
    },
    # script.ai_action_music is entity-disabled → not in states
]


class FakeRest:
    async def list_states(self):
        return STATES


class FakeWS:
    connected = True
    last_management_call = None

    async def request(self, msg_type, **payload):
        if msg_type == "config/entity_registry/list":
            return ENTITY_REGISTRY
        raise RuntimeError(f"unexpected request: {msg_type}")

    async def management_request(self, msg_type, **payload):
        self.last_management_call = {"type": msg_type, **payload}
        return None


def _client_with_fakes(app, ws=None):
    fake_ws = ws or FakeWS()
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = fake_ws
        yield client, fake_ws


def test_get_actions_returns_ai_automations_and_scripts():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = FakeWS()
        resp = client.get("/api/actions")
    assert resp.status_code == 200
    entity_ids = {a["entity_id"] for a in resp.json()["actions"]}
    assert entity_ids == {
        "automation.ai_night_lights",
        "automation.ai_away_mode",
        "script.ai_action_lights_on",
        "script.ai_action_music",
    }
    # non-AI entities excluded
    assert "automation.morning_routine" not in entity_ids
    assert "light.kitchen" not in entity_ids


def test_get_actions_automation_enabled_reflects_state():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = FakeWS()
        resp = client.get("/api/actions")
    actions = {a["entity_id"]: a for a in resp.json()["actions"]}
    assert actions["automation.ai_night_lights"]["enabled"] is True
    assert actions["automation.ai_away_mode"]["enabled"] is False


def test_get_actions_disabled_script_appears_with_enabled_false():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = FakeWS()
        resp = client.get("/api/actions")
    actions = {a["entity_id"]: a for a in resp.json()["actions"]}
    assert actions["script.ai_action_music"]["enabled"] is False
    assert actions["script.ai_action_lights_on"]["enabled"] is True


def test_get_actions_type_field_set_correctly():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = FakeWS()
        resp = client.get("/api/actions")
    actions = {a["entity_id"]: a for a in resp.json()["actions"]}
    assert actions["automation.ai_night_lights"]["type"] == "automation"
    assert actions["script.ai_action_lights_on"]["type"] == "script"


def test_get_actions_returns_503_without_websocket():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.ws = None
        resp = client.get("/api/actions")
    assert resp.status_code == 503


def test_toggle_automation_enable_calls_turn_on():
    fake_ws = FakeWS()
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = fake_ws
        resp = client.post(
            "/api/actions/automation.ai_night_lights/toggle",
            json={"enabled": True},
        )
    assert resp.status_code == 200
    assert fake_ws.last_management_call["type"] == "call_service"
    assert fake_ws.last_management_call["domain"] == "automation"
    assert fake_ws.last_management_call["service"] == "turn_on"


def test_toggle_automation_disable_calls_turn_off():
    fake_ws = FakeWS()
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = fake_ws
        resp = client.post(
            "/api/actions/automation.ai_away_mode/toggle",
            json={"enabled": False},
        )
    assert resp.status_code == 200
    assert fake_ws.last_management_call["service"] == "turn_off"


def test_toggle_script_enable_clears_disabled_by():
    fake_ws = FakeWS()
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = fake_ws
        resp = client.post(
            "/api/actions/script.ai_action_music/toggle",
            json={"enabled": True},
        )
    assert resp.status_code == 200
    assert fake_ws.last_management_call["type"] == "config/entity_registry/update"
    assert fake_ws.last_management_call["disabled_by"] is None


def test_toggle_script_disable_sets_disabled_by_user():
    fake_ws = FakeWS()
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = fake_ws
        resp = client.post(
            "/api/actions/script.ai_action_lights_on/toggle",
            json={"enabled": False},
        )
    assert resp.status_code == 200
    assert fake_ws.last_management_call["disabled_by"] == "user"


def test_toggle_rejects_non_ai_entity():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.rest = FakeRest()
        client.app.state.ws = FakeWS()
        resp = client.post(
            "/api/actions/automation.morning_routine/toggle",
            json={"enabled": False},
        )
    assert resp.status_code == 403


def test_toggle_returns_503_without_websocket():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.ws = None
        resp = client.post(
            "/api/actions/automation.ai_night_lights/toggle",
            json={"enabled": True},
        )
    assert resp.status_code == 503
