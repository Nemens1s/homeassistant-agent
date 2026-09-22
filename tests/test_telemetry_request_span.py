"""Tests for Task 13: request root span, request_id in responses."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.main import ChatResponse


def test_chatresponse_has_optional_request_id():
    assert ChatResponse(reply="hi").request_id is None
    assert ChatResponse(reply="hi", request_id="abc").request_id == "abc"


# ---------------------------------------------------------------------------
# Endpoint-level test: /api/chat returns non-null request_id when telemetry
# is enabled with a real temp DB.  We construct the app with a Settings that
# points at a writable tmp file, install a FakeAgent on app.state, and assert
# that the response JSON contains a non-null "request_id".
#
# OTel provider-override hygiene: init_telemetry calls trace.set_tracer_provider
# which is a global mutation.  To avoid the "Overriding of current
# TracerProvider is not allowed" warning we reset the global before AND after
# this test via the `reset_otel_provider` fixture below.
# ---------------------------------------------------------------------------


@pytest.fixture()
def reset_otel_provider():
    """Reset the OTel global TracerProvider before and after the test.

    This keeps the full suite at exactly 1 warning (the starlette one).
    The reset is done by setting the internal ``_TRACER_PROVIDER`` to None,
    which is the documented / published reset path in opentelemetry-api.
    """
    from opentelemetry import trace as otel_trace

    # Save whatever was set before (may be NoOpTracerProvider or real SDK provider).
    _orig = otel_trace._TRACER_PROVIDER  # noqa: SLF001

    # Reset before the test so init_telemetry can set a fresh one.
    otel_trace._TRACER_PROVIDER = None  # noqa: SLF001

    yield

    # Restore after the test to avoid polluting other tests.
    otel_trace._TRACER_PROVIDER = _orig  # noqa: SLF001


def test_chat_returns_request_id_when_telemetry_enabled(tmp_path, reset_otel_provider):
    """POST /api/chat should return a non-null request_id in the JSON body
    when telemetry is enabled and the DB is writable."""
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        telemetry_enabled=True,
        telemetry_db_path=str(tmp_path / "telemetry.sqlite"),
    )

    app = create_app(settings)

    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            return {"messages": [AIMessage(content="hello from agent")]}

    with TestClient(app) as client:
        client.app.state.agent = FakeAgent()
        resp = client.post("/api/chat", json={"message": "what time is it?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "hello from agent"
    assert body["request_id"] is not None
    # request_id should be a 32-hex-char trace ID
    assert len(body["request_id"]) == 32
    assert all(c in "0123456789abcdef" for c in body["request_id"])
