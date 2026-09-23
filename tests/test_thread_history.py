from types import SimpleNamespace

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.history import display_transcript
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


# --- display_transcript (pure) ---------------------------------------------
def test_display_transcript_reconstructs_user_and_bot():
    messages = [HumanMessage("goodnight"), AIMessage("Done — see you.")]
    assert display_transcript(messages) == [
        {"role": "user", "text": "goodnight"},
        {"role": "bot", "text": "Done — see you."},
    ]


def test_display_transcript_strips_thinking():
    messages = [AIMessage("<think>plan</think>the answer is 5")]
    assert display_transcript(messages) == [{"role": "bot", "text": "the answer is 5"}]


def test_display_transcript_skips_tool_calls_and_tool_messages():
    tool_call = AIMessage(
        content="",
        tool_calls=[{"name": "trigger_action", "args": {"entity_id": "x"}, "id": "c1"}],
    )
    tool_msg = ToolMessage(content='{"status":"ok"}', tool_call_id="c1", name="trigger_action")
    messages = [
        HumanMessage("goodnight"),
        tool_call,
        tool_msg,
        AIMessage("Done — triggered automation.ai_action_night."),
    ]
    assert display_transcript(messages) == [
        {"role": "user", "text": "goodnight"},
        {"role": "bot", "text": "Done — triggered automation.ai_action_night."},
    ]


# --- GET /api/history -------------------------------------------------------
class FakeStateAgent:
    def __init__(self, by_thread):
        self._by_thread = by_thread

    async def aget_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        messages = self._by_thread.get(thread_id, [])
        return SimpleNamespace(values={"messages": messages} if messages else {})


def test_history_endpoint_returns_transcript():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeStateAgent(
            {"t1": [HumanMessage("hi"), AIMessage("hello there")]}
        )
        resp = client.get("/api/history", params={"thread_id": "t1"})
    assert resp.status_code == 200
    assert resp.json() == {
        "messages": [
            {"role": "user", "text": "hi"},
            {"role": "bot", "text": "hello there"},
        ]
    }


def test_history_endpoint_unknown_thread_is_empty():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeStateAgent({})
        resp = client.get("/api/history", params={"thread_id": "nope"})
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}
