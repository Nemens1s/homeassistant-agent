"""Shared pytest fixtures.

Clears all env vars that Settings reads so tests that construct
Settings(_env_file=None) get true defaults regardless of the developer's .env.
"""

import pytest

_SETTINGS_ENV_VARS = [
    "HA_BASE_URL",
    "HA_TOKEN",
    "SUPERVISOR_TOKEN",
    "WS_CONNECT_TIMEOUT",
    "LLM_PROVIDER",
    "OLLAMA_URL",
    "LLM_MODEL",
    "OLLAMA_MODEL",  # legacy, ignored by Settings but may be present
    "API_KEY",
    "TEMPERATURE",
    "SEED",
    "REASONING",
    "NUM_PREDICT",
    "NUM_GPU",
    "NUM_CTX",
    "KEEP_ALIVE",
    "SHOW_THINKING",
    "SYSTEM_PROMPT",
    "MAX_TIER",
    "ALLOWED_DOMAINS",
    "ALLOWED_LABELS",
    "RECURSION_LIMIT",
    "MAX_ROWS",
    "AUDIT_DB_PATH",
    "ENABLE_TOOL_SUBSETTING",
]


@pytest.fixture(autouse=True)
def _clear_settings_env(monkeypatch):
    for var in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
