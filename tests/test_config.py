import json

import pytest

from app.config import Settings, load_settings


@pytest.fixture(autouse=True)
def _clean_token_env(monkeypatch):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)


def test_defaults():
    s = Settings(_env_file=None)
    assert s.ha_base_url == "http://supervisor/core"
    assert s.llm_provider == "ollama"
    assert s.llm_model == "hf.co/empero-ai/Qwen3.8-2B-GGUF:Q6_K"
    assert s.max_tier == 1
    assert s.temperature == 0.0
    assert s.reasoning is False
    assert s.num_ctx == 8192
    assert s.ai_actions_switch == "input_boolean.ai_triggered_actions"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "tok123")
    monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
    s = Settings(_env_file=None)
    assert s.ha_token == "tok123"
    assert s.llm_model == "qwen3:8b"


def test_supervisor_token_alias(monkeypatch):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supertok")
    s = Settings(_env_file=None)
    assert s.ha_token == "supertok"


def test_ws_url_supervisor():
    s = Settings(_env_file=None, ha_base_url="http://supervisor/core")
    assert s.ws_url == "ws://supervisor/core/websocket"


def test_ws_url_direct():
    s = Settings(_env_file=None, ha_base_url="http://192.168.1.10:8123")
    assert s.ws_url == "ws://192.168.1.10:8123/api/websocket"


def test_needle_defaults_are_off_and_safe():
    s = Settings(_env_file=None)
    assert s.needle_enabled is False
    assert s.needle_confidence_threshold == 0.0  # trust-the-call (tuned weights have no confidence)
    assert s.needle_menu_ttl_s == 60
    assert s.needle_remote_url == ""


def test_load_settings_from_options_json(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"llm_model": "llama3.1:8b", "num_ctx": 4096}))
    monkeypatch.setattr("app.config.OPTIONS_FILE", options)
    s = load_settings()
    assert s.llm_model == "llama3.1:8b"
    assert s.num_ctx == 4096


def test_script_domain_allowed_by_default():
    s = Settings(_env_file=None)
    assert "script" in s.allowed_domains


from pathlib import Path

import yaml

_EVENT_MEMORY_KEYS = (
    "watched_entities",
    "clock_entity",
    "event_confirmations_enabled",
    "memory_note_default_ttl_hours",
    "memory_note_max_ttl_hours",
    "time_note_grace_minutes",
    "notify_action",
)


def test_event_memory_defaults():
    s = Settings(_env_file=None)
    assert s.watched_entities == []
    assert s.clock_entity == "sensor.europe_tallinn"
    assert s.event_confirmations_enabled is True
    assert s.memory_note_default_ttl_hours == 24
    assert s.memory_note_max_ttl_hours == 168
    assert s.time_note_grace_minutes == 120
    assert s.notify_action == "script.ai_action_notify"


def test_addon_config_mirrors_event_memory_settings():
    config_path = Path(__file__).parent.parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    for key in _EVENT_MEMORY_KEYS:
        assert key in config["options"], key
        assert key in config["schema"], key
