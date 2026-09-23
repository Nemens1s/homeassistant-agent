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
