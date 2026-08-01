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
        ollama_url="http://127.0.0.1:59998",
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
    assert body["ollama"] is False
    assert body["websocket"] is False


def test_chat_returns_agent_reply():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeAgent()
        resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    assert resp.json() == {"reply": "hi there"}


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
