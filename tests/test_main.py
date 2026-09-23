import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

from app.config import Settings
from app.main import create_app


def _settings():
    # Nothing listens on these ports: lifespan must degrade, not crash.
    return Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
    )


class FakeAgent:
    async def ainvoke(self, payload, config=None):
        return {"messages": [AIMessage(content="hi there")]}


class FakeAgentRecursion:
    async def ainvoke(self, payload, config=None):
        raise GraphRecursionError("Recursion limit reached")


def test_health_degraded_without_backends():
    app = create_app(_settings())
    with TestClient(app) as client:
        resp = client.get("/api/health")
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "degraded"
    assert body["ha"] is False
    assert body["llm"] is False
    assert body["websocket"] is False


def test_chat_returns_agent_reply():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeAgent()
        resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "hi there"


def test_chat_returns_graceful_reply_on_recursion_limit():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeAgentRecursion()
        resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    assert "stopped after" in resp.json()["reply"]


def test_frontend_served_at_root():
    app = create_app(_settings())
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "html" in resp.headers["content-type"]


def test_lifespan_wires_audit_and_write_domains(monkeypatch, tmp_path):
    captured = {}

    def fake_build_agent(settings, ctx, checkpointer=None, fast_path=None, telemetry=None):
        captured["audit"] = ctx.audit
        captured["rest"] = ctx.rest
        class A:
            async def ainvoke(self, *a, **k):
                return {"messages": []}
        return A()

    from app import main as main_mod
    monkeypatch.setattr(main_mod, "build_agent", fake_build_agent)
    settings = _settings()
    settings.max_tier = 2
    settings.audit_db_path = str(tmp_path / "a.db")
    app = create_app(settings)
    with TestClient(app):
        pass
    assert captured["audit"] is not None
    assert captured["rest"]._allowed_write_domains == ("light", "switch", "fan", "automation", "script")


def test_lifespan_wires_durable_checkpointer(monkeypatch, tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    captured = {}

    def fake_build_agent(settings, ctx, checkpointer=None, fast_path=None, telemetry=None):
        captured["checkpointer"] = checkpointer
        class A:
            async def ainvoke(self, *a, **k):
                return {"messages": []}
        return A()

    from app import main as main_mod
    monkeypatch.setattr(main_mod, "build_agent", fake_build_agent)
    settings = _settings()
    db = tmp_path / "checkpoints.sqlite"
    settings.checkpoint_db_path = str(db)
    app = create_app(settings)
    with TestClient(app):
        pass
    assert isinstance(captured["checkpointer"], AsyncSqliteSaver)


def _parse_sse(text):
    return [json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")]


class FakeStreamAgent:
    async def astream(self, payload, config=None, stream_mode=None):
        from langchain_core.messages import AIMessageChunk
        yield AIMessageChunk(content="hel"), {}
        yield AIMessageChunk(content="lo"), {}


def test_chat_stream_emits_protocol_events():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeStreamAgent()
        resp = client.post("/api/chat/stream", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert events[0] == {"type": "token", "text": "hel"}
    last = events[-1]
    assert last["type"] == "done"
    assert last["reply"] == "hello"
    # Telemetry is disabled here (no-op tracer, trace_id == 0), so request_id
    # is None — never the phantom all-zeros key that would collide in the label store.
    assert last["request_id"] is None


def test_chat_stream_runs_agent_within_root_span(tmp_path, reset_otel_provider):
    # The agent graph must run while the invoke_agent root span is CURRENT, so
    # child spans (chat, execute_tool, fast_path.classify) parent onto it and
    # share its trace_id — otherwise each orphan span becomes its own trace and
    # the exporter writes phantom empty `requests` rows.
    from opentelemetry import trace as ot
    from langchain_core.messages import AIMessageChunk

    captured = {}

    class SpanCheckAgent:
        async def astream(self, payload, config=None, stream_mode=None):
            span = ot.get_current_span()
            captured["trace_id"] = format(span.get_span_context().trace_id, "032x")
            yield AIMessageChunk(content="ok"), {}

    settings = _settings()
    # A real telemetry DB path installs a real recording provider (non-zero
    # trace ids), so we can compare the in-graph trace id to the request_id.
    settings.telemetry_db_path = str(tmp_path / "t.sqlite")
    app = create_app(settings)
    with TestClient(app) as client:
        client.app.state.agent = SpanCheckAgent()
        resp = client.post("/api/chat/stream", json={"message": "hi"})
    done = _parse_sse(resp.text)[-1]
    assert done["type"] == "done"
    # Agent ran INSIDE the root span → same trace id as the emitted request_id.
    assert captured["trace_id"] == done["request_id"]


def test_chat_stream_recursion_limit_is_error_event():
    from langgraph.errors import GraphRecursionError

    class Exploding:
        async def astream(self, payload, config=None, stream_mode=None):
            raise GraphRecursionError("limit")
            yield  # pragma: no cover — makes this an async generator

    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = Exploding()
        resp = client.post("/api/chat/stream", json={"message": "hi"})
    events = _parse_sse(resp.text)
    assert events[-1]["type"] == "error"


async def test_lifespan_teardown_survives_rest_close_failure():
    # ws.stop must run even if rest.aclose raises
    from app import main as main_mod

    calls = []

    class BadRest:
        async def aclose(self):
            calls.append("rest")
            raise RuntimeError("boom")

    class GoodWS:
        connected = False

        async def stop(self):
            calls.append("ws")

    # exercise the teardown helper directly
    with pytest.raises(RuntimeError):
        await main_mod._teardown(BadRest(), GoodWS())
    assert calls == ["rest", "ws"]


def test_lifespan_builds_fast_path_when_needle_enabled(monkeypatch):
    # Regression: build_fast_path_backend guards on registry.get("trigger_action"),
    # so the tool registry must be loaded BEFORE it runs in the lifespan. If it isn't,
    # the backend builds to None and FastPathMiddleware is silently never added.
    from app.tools import registry

    registry._reset_for_tests()
    captured = {}

    def fake_build_agent(settings, ctx, checkpointer=None, fast_path=None, telemetry=None):
        # Does NOT call registry.load_all(), so only the lifespan can load it.
        captured["fast_path"] = fast_path
        class A:
            async def ainvoke(self, *a, **k):
                return {"messages": []}
        return A()

    from app import main as main_mod
    monkeypatch.setattr(main_mod, "build_agent", fake_build_agent)
    settings = _settings()
    settings.max_tier = 2
    settings.needle_enabled = True
    settings.needle_remote_url = "http://needle.lan:8765"
    app = create_app(settings)
    with TestClient(app):
        pass
    assert captured["fast_path"] is not None
    registry._reset_for_tests()
