import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.agent.run_scope import AREA_ID_KEY, PERSON_ENTITY_KEY, RequestFacts
from app.api import chat as chat_mod
from app.config import Settings
from app.main import create_app
from app.telemetry import conventions as C

_FACTS = RequestFacts("bedroom", "Bedroom", "Ilja", "person.ilja")
_CONTEXT = {"device_id": "d1", "satellite_id": "assist_satellite.living_room", "user_id": "u1"}


def _settings(**overrides):
    return Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        clock_entity="sensor.europe_tallinn",
        **overrides,
    )


class RecordingAgent:
    def __init__(self):
        self.configs = []

    async def ainvoke(self, payload, config=None):
        self.configs.append(config)
        return {"messages": [AIMessage(content="hi there")]}


class RecordingStreamAgent:
    def __init__(self):
        self.configs = []

    async def astream(self, payload, config=None, stream_mode=None):
        self.configs.append(config)
        yield AIMessageChunk(content="ok"), {}


def _fake_resolver(calls, facts=_FACTS):
    async def resolve(ctx, device_id, user_id):
        calls.append((device_id, user_id))
        return facts
    return resolve


def _post(body, agent, monkeypatch=None, resolver=None, path="/api/chat"):
    if resolver is not None:
        monkeypatch.setattr(chat_mod, "resolve_request_facts", resolver)
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = agent
        return client.post(path, json=body)


def test_old_payload_still_accepted():
    agent = RecordingAgent()
    resp = _post({"message": "hello"}, agent)
    assert resp.status_code == 200
    assert resp.json()["reply"] == "hi there"
    assert AREA_ID_KEY not in agent.configs[0]["configurable"]


def test_unknown_context_keys_ignored(monkeypatch):
    agent = RecordingAgent()
    resp = _post({"message": "hello", "context": {"device_id": "d", "foo": 1}}, agent,
                 monkeypatch, _fake_resolver([]))
    assert resp.status_code == 200


def test_context_facts_reach_configurable(monkeypatch):
    calls = []
    agent = RecordingAgent()
    _post({"message": "hello", "channel": "assist", "context": _CONTEXT}, agent,
          monkeypatch, _fake_resolver(calls))
    configurable = agent.configs[0]["configurable"]
    assert configurable[AREA_ID_KEY] == "bedroom"
    assert configurable[PERSON_ENTITY_KEY] == "person.ilja"
    assert calls == [("d1", "u1")]


def test_slow_resolution_is_dropped(monkeypatch):
    async def slow(ctx, device_id, user_id):
        await asyncio.sleep(5)
        return _FACTS

    agent = RecordingAgent()
    started = time.monotonic()
    resp = _post({"message": "hello", "context": _CONTEXT}, agent, monkeypatch, slow)
    assert resp.status_code == 200
    assert time.monotonic() - started < 3
    assert AREA_ID_KEY not in agent.configs[0]["configurable"]


def test_resolver_error_is_dropped(monkeypatch):
    async def boom(ctx, device_id, user_id):
        raise RuntimeError("boom")

    agent = RecordingAgent()
    resp = _post({"message": "hello", "context": _CONTEXT}, agent, monkeypatch, boom)
    assert resp.status_code == 200
    assert AREA_ID_KEY not in agent.configs[0]["configurable"]


def test_stream_endpoint_forwards_facts(monkeypatch):
    calls = []
    agent = RecordingStreamAgent()
    resp = _post({"message": "hello", "channel": "ui", "context": _CONTEXT}, agent,
                 monkeypatch, _fake_resolver(calls), path="/api/chat/stream")
    assert resp.status_code == 200
    assert agent.configs[0]["configurable"][AREA_ID_KEY] == "bedroom"
    assert calls == [("d1", "u1")]


def test_root_span_records_context(monkeypatch, reset_otel_provider):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    from opentelemetry import trace as otel_trace
    otel_trace.set_tracer_provider(provider)

    monkeypatch.setattr(chat_mod, "resolve_request_facts", _fake_resolver([]))
    app = create_app(_settings(telemetry_enabled=False))
    with TestClient(app) as client:
        client.app.state.agent = RecordingAgent()
        client.post("/api/chat", json={"message": "hello", "context": _CONTEXT})

    root = [s for s in exporter.get_finished_spans() if s.name == C.SPAN_INVOKE_AGENT][0]
    assert root.attributes[C.GOSLING_CHANNEL] == "assist"
    assert root.attributes[C.GOSLING_DEVICE_ID] == "d1"
    assert root.attributes[C.GOSLING_SATELLITE_ID] == "assist_satellite.living_room"
    assert root.attributes[C.GOSLING_CONTEXT_AREA] == "bedroom"
    assert root.attributes[C.GOSLING_CONTEXT_PERSON] == "person.ilja"


def test_frontend_sends_ui_channel():
    assert "channel: 'ui'" in Path("frontend/app.js").read_text()
