import json

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk

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


class OutageAgent:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        raise httpx.ConnectError("refused")

    async def astream(self, payload, config=None, stream_mode=None):
        self.calls += 1
        yield (AIMessageChunk(content="Part"), {})
        raise httpx.ConnectError("refused")


class CountingAgent:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        return {"messages": [AIMessage(content="agent reply")]}


class CountingLanguage:
    enabled = True

    def __init__(self):
        self.inbound_calls = 0

    async def inbound(self, text, thread_id):
        self.inbound_calls += 1
        raise AssertionError("lang-mt must not be called while the LLM is down")

    async def outbound(self, reply_en, inbound):
        raise AssertionError("lang-mt must not be called while the LLM is down")


def _sse_events(text):
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data:"):
            events.append(json.loads(frame[len("data:"):]))
    return events


def _stop_probes(client):
    client.portal.call(client.app.state.llm_health.stop)


def test_chat_outage_returns_degraded_reply_and_marks_down():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        client.app.state.agent = OutageAgent()
        resp = client.post("/api/chat", json={"message": "what's the temperature?"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["degraded"] is True
        assert "language model is offline" in body["reply"]
        assert client.app.state.llm_health.state == "down"


def test_message_while_down_never_calls_agent_or_lang_mt():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        agent = CountingAgent()
        language = CountingLanguage()
        client.app.state.agent = agent
        client.app.state.language = language
        client.app.state.llm_health.mark_down("refused")
        body = client.post("/api/chat", json={"message": "hello"}).json()
        assert body["degraded"] is True
        assert agent.calls == 0
        assert language.inbound_calls == 0


def test_agent_called_again_after_recovery():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        agent = CountingAgent()
        client.app.state.agent = agent
        client.app.state.llm_health.mark_down("refused")
        client.portal.call(client.app.state.llm_health.mark_up)
        body = client.post("/api/chat", json={"message": "hello"}).json()
        assert body["reply"] == "agent reply"
        assert body["degraded"] is False
        assert agent.calls == 1


def test_slash_command_never_reaches_agent():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        agent = CountingAgent()
        client.app.state.agent = agent
        body = client.post("/api/chat", json={"message": "/help"}).json()
        assert body["degraded"] is True
        assert "/actions" in body["reply"]
        assert agent.calls == 0


def test_router_crash_returns_offline_reply():
    class Exploding:
        async def handle(self, text, thread_id):
            raise RuntimeError("router bug")

    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        client.app.state.router = Exploding()
        resp = client.post("/api/chat", json={"message": "/help"})
        assert resp.status_code == 200
        assert "language model is offline" in resp.json()["reply"]


def test_stream_outage_ends_with_degraded_done():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        client.app.state.agent = OutageAgent()
        resp = client.post("/api/chat/stream", json={"message": "what's the temperature?"})
        events = _sse_events(resp.text)
        assert events[-1]["type"] == "done"
        assert events[-1]["degraded"] is True
        assert "language model is offline" in events[-1]["reply"]
        assert client.app.state.llm_health.state == "down"


def test_stream_slash_command():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        resp = client.post("/api/chat/stream", json={"message": "/help"})
        events = _sse_events(resp.text)
        assert events[-1]["type"] == "done"
        assert events[-1]["degraded"] is True
        assert "/actions" in events[-1]["reply"]
