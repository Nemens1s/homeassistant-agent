import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage


class _Agent:
    def __init__(self, items): self._items = items
    async def astream(self, *a, **k):
        for it in self._items:
            yield it


@pytest.mark.asyncio
async def test_stream_emits_complete_aimessage():
    from app.agent.streaming import stream_events
    full = AIMessage(content="Done — triggered automation.ai_action_night.")
    agent = _Agent([(full, {})])
    events = [e async for e in stream_events(agent, "goodnight", "t1", 15)]
    texts = "".join(e["text"] for e in events if e["type"] == "token")
    assert "automation.ai_action_night" in texts
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_stream_does_not_duplicate_chunks():
    from app.agent.streaming import stream_events
    chunks = [(AIMessageChunk(content="Hel"), {}), (AIMessageChunk(content="lo"), {})]
    agent = _Agent(chunks)
    events = [e async for e in stream_events(agent, "hi", "t1", 15)]
    assert events[-1]["reply"] == "Hello"


@pytest.mark.asyncio
async def test_templated_action_reply_streams_tool_frames_in_order():
    from app.agent.streaming import stream_events
    call = AIMessage(content="", tool_calls=[{"name": "trigger_action",
                                              "args": {"entity_id": "script.ai_action_lights_on"},
                                              "id": "call-1"}])
    result = ToolMessage(content='{"status":"ok"}', tool_call_id="call-1", name="trigger_action")
    reply = AIMessage(content="Done — triggered Lights On.",
                      response_metadata={"templated_reply": True})
    agent = _Agent([(call, {}), (result, {}), (reply, {})])
    events = [e async for e in stream_events(agent, "turn on the lights", "t1", 15)]
    types = [e["type"] for e in events if e["type"] in ("tool_call", "tool_result", "done")]
    assert types == ["tool_call", "tool_result", "done"]
    assert events[-1]["reply"] == "Done — triggered Lights On."
