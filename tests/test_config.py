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
    assert s.llm_provider == "auto"
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


def test_fast_path_defaults_are_off_and_safe():
    s = Settings(_env_file=None)
    assert s.fast_path_enabled is False
    assert s.fast_path_confidence_threshold == 0.0  # trust-the-call (tuned weights have no confidence)
    assert s.fast_path_url == ""


def test_fast_path_keys_and_legacy_needle_aliases():
    s = Settings(_env_file=None, fast_path_enabled=True, fast_path_url="http://x")
    assert s.fast_path_enabled and s.fast_path_url == "http://x"
    legacy = Settings(_env_file=None, needle_enabled=True, needle_remote_url="http://y",
                      needle_confidence_threshold=0.4)
    assert legacy.fast_path_enabled is True
    assert legacy.fast_path_url == "http://y"
    assert legacy.fast_path_confidence_threshold == 0.4


def test_load_settings_from_options_json(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"llm_model": "llama3.1:8b", "num_ctx": 4096}))
    monkeypatch.setattr("app.config.OPTIONS_FILE", options)
    s = load_settings()
    assert s.llm_model == "llama3.1:8b"
    assert s.num_ctx == 4096


def test_allow_actions_default_off():
    s = Settings(_env_file=None)
    assert s.allow_actions is False
    assert s.max_tier == 1


def test_allow_actions_maps_to_tier_2():
    assert Settings(_env_file=None, allow_actions=True).max_tier == 2


def test_legacy_max_tier_key_still_works():
    assert Settings(_env_file=None, max_tier=2).allow_actions is True
    assert Settings(_env_file=None, max_tier=1).allow_actions is False


def test_allow_actions_wins_over_legacy_max_tier():
    assert Settings(_env_file=None, allow_actions=False, max_tier=2).allow_actions is False


def test_allowed_domains_is_gone_and_ignored():
    s = Settings(_env_file=None, allowed_domains=["light"])
    assert not hasattr(s, "allowed_domains")


def test_dead_options_are_gone():
    s = Settings(_env_file=None)
    assert not hasattr(s, "otlp_endpoint")
    assert not hasattr(s, "telemetry_retention_days")
    assert not hasattr(s, "needle_menu_ttl_s")


def test_old_options_json_with_dead_keys_still_loads(tmp_path, monkeypatch):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"otlp_endpoint": "", "telemetry_retention_days": 0,
                                "needle_menu_ttl_s": 60}))
    monkeypatch.setattr("app.config.OPTIONS_FILE", opts)
    load_settings()  # must not raise


def test_shipped_addon_options_allow_actions_off_by_default():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    assert cfg["options"]["allow_actions"] is False
    assert "allowed_domains" not in cfg["options"]
    assert "max_tier" not in cfg["options"]


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
    assert s.clock_entity == ""
    assert s.event_confirmations_enabled is True
    assert s.memory_note_default_ttl_hours == 24
    assert s.memory_note_max_ttl_hours == 168
    assert s.time_note_grace_minutes == 120
    assert s.notify_action == "script.ai_action_notify"


def test_addon_config_mirrors_event_memory_settings():
    config_path = Path(__file__).parent.parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    _VISIBLE_EVENT_MEMORY_KEYS = (
        "watched_entities", "clock_entity", "event_confirmations_enabled", "notify_action",
    )
    for key in _EVENT_MEMORY_KEYS:
        assert key in config["schema"], key
        if key in _VISIBLE_EVENT_MEMORY_KEYS:
            assert key in config["options"], key


import pytest
from pydantic import ValidationError


@pytest.mark.parametrize("field", [
    "time_note_grace_minutes", "memory_note_default_ttl_hours", "memory_note_max_ttl_hours",
])
def test_scheduling_durations_must_be_positive(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_notify_action_is_normalised():
    s = Settings(_env_file=None, notify_action="  Script.AI_Action_Notify ")
    assert s.notify_action == "script.ai_action_notify"


def test_visible_addon_options_are_minimal():
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    assert set(cfg["options"]) == {
        "llm_url", "llm_model", "api_key", "allow_actions", "ai_actions_switch",
        "notify_action", "watched_entities", "clock_entity",
        "event_confirmations_enabled", "person_name_map",
        "language_layer_enabled", "languages",
    }
    for key in cfg["schema"]:
        if key not in cfg["options"]:
            assert str(cfg["schema"][key]).endswith("?") or isinstance(cfg["schema"][key], list), key


def test_every_schema_key_is_a_settings_field():
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    fields = set(Settings.model_fields)
    for key in cfg["schema"]:
        assert key in fields, key


def test_paths_come_from_env_when_absent_from_options(tmp_path, monkeypatch):
    opts = tmp_path / "options.json"
    opts.write_text("{}")
    monkeypatch.setattr("app.config.OPTIONS_FILE", opts)
    monkeypatch.setenv("CHECKPOINT_DB_PATH", "/data/checkpoints.sqlite")
    assert load_settings().checkpoint_db_path == "/data/checkpoints.sqlite"
