import json

from app.config import Settings, load_settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.ha_base_url == "http://supervisor/core"
    assert s.llm_provider == "ollama"
    assert s.llm_model == "qwen2.5:7b"
    assert s.max_tier == 1
    assert s.temperature == 0.0
    assert s.reasoning is False
    assert s.num_ctx == 8192


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


def test_load_settings_from_options_json(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"llm_model": "llama3.1:8b", "num_ctx": 4096}))
    monkeypatch.setattr("app.config.OPTIONS_FILE", options)
    s = load_settings()
    assert s.llm_model == "llama3.1:8b"
    assert s.num_ctx == 4096
