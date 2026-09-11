import json

from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError

from app.agent.streaming import stream_events


class FakeAgent:
    def __init__(self, script):
        self._script = script

    async def astream(self, payload, config=None, stream_mode=None):
        assert stream_mode == "messages"
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item


async def _collect(agent):
    return [e async for e in stream_events(agent, "hi", "t1", 15)]


async def test_token_and_done_events():
    agent = FakeAgent([
        (AIMessageChunk(content="The "), {}),
        (AIMessageChunk(content="light is on."), {}),
    ])
    events = await _collect(agent)
    assert events[0] == {"type": "token", "text": "The "}
    assert events[-1] == {"type": "done", "reply": "The light is on."}


async def test_tool_call_and_result_events():
    chunk = AIMessageChunk(content="")
    chunk.tool_calls = [{"name": "get_entity_state",
                         "args": {"entity_id": "light.kitchen"}, "id": "c1"}]
    tool_msg = ToolMessage(content=json.dumps({"status": "ok", "data": {}}),
                           tool_call_id="c1", name="get_entity_state")
    agent = FakeAgent([(chunk, {}), (tool_msg, {}),
                       (AIMessageChunk(content="done"), {})])
    events = await _collect(agent)
    types = [e["type"] for e in events]
    assert types == ["tool_call", "tool_result", "token", "done"]
    assert events[0]["name"] == "get_entity_state"
    assert events[1]["status"] == "ok"


async def test_recursion_error_becomes_terminal_error_event():
    agent = FakeAgent([(AIMessageChunk(content="thinking"), {}),
                       GraphRecursionError("limit")])
    events = await _collect(agent)
    assert events[-1]["type"] == "error"
    assert "15" in events[-1]["message"]
    assert not any(e["type"] == "done" for e in events)


async def test_tool_result_error_status():
    tool_msg = ToolMessage(content=json.dumps({"status": "error",
                                               "error": {"code": "x", "message": "y"}}),
                           tool_call_id="c1", name="get_history")
    agent = FakeAgent([(tool_msg, {}), (AIMessageChunk(content="sorry"), {})])
    events = await _collect(agent)
    assert events[0] == {"type": "tool_result", "name": "get_history", "status": "error"}
