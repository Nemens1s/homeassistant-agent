"""Shared pytest fixtures.

Clears all env vars that Settings reads so tests that construct
Settings(_env_file=None) get true defaults regardless of the developer's .env.
"""

import pytest


@pytest.fixture()
def reset_otel_provider():
    """Reset the OTel global TracerProvider + set-once latch before/after a test.

    Resets both ``_TRACER_PROVIDER`` and ``_TRACER_PROVIDER_SET_ONCE._done`` so
    that ``trace.set_tracer_provider`` works even when a previous test already
    set a real provider.  This keeps the full suite at exactly 1 warning (the
    starlette deprecation warning).
    """
    from opentelemetry import trace as otel_trace

    _orig = otel_trace._TRACER_PROVIDER  # noqa: SLF001
    _orig_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # noqa: SLF001

    otel_trace._TRACER_PROVIDER = None  # noqa: SLF001
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001

    yield

    otel_trace._TRACER_PROVIDER = _orig  # noqa: SLF001
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = _orig_done  # noqa: SLF001


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
    "CHECKPOINT_DB_PATH",
    "MAX_HISTORY_MESSAGES",
    "ENABLE_TOOL_SUBSETTING",
    "NEEDLE_ENABLED",
    "NEEDLE_MODEL_PATH",
    "NEEDLE_CONFIDENCE_THRESHOLD",
    "NEEDLE_MENU_TTL_S",
    "NEEDLE_BACKEND",
    "NEEDLE_SIDECAR_URL",
    "PERSON_NAME_MAP",
    "PERSON_NAME_EXCLUDE",
]


@pytest.fixture(autouse=True)
def _clear_settings_env(monkeypatch):
    for var in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
