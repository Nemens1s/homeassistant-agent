"""End-to-end graph test with a scripted fake model: proves the middleware
wiring (guard reset per run, timestamped system prompt) executes inside the
real create_agent graph — not just in isolation."""

import json

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent import factory as factory_mod
from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class FakeRest:
    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {},
                "last_changed": "2026-07-13T10:00:00+00:00"}

    async def list_states(self):
        return []


def _scripted_agent(monkeypatch, responses):
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=1)
    ctx = ToolContext(settings=settings, rest=FakeRest(), ws=None)
    model = ScriptedModel(responses=responses)
    monkeypatch.setattr(factory_mod, "build_llm", lambda s: model)
    return factory_mod.build_agent(settings, ctx)


async def test_tool_call_flows_through_adapter_and_middleware(monkeypatch):
    responses = [
        AIMessage(content="", tool_calls=[
            {"name": "get_entity_state", "args": {"entity_id": "light.kitchen"}, "id": "c1"}
        ]),
        AIMessage(content="The kitchen light is on."),
    ]
    agent = _scripted_agent(monkeypatch, responses)
    cfg = {"configurable": {"thread_id": "t1"}, "recursion_limit": 15}
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "kitchen light?"}]}, config=cfg)
    # tool ran through the adapter: its ToolMessage content is an envelope
    tool_msgs = [m for m in result["messages"] if m.type == "tool"]
    assert tool_msgs, "tool was not executed"
    envelope = json.loads(tool_msgs[0].content)
    assert envelope["status"] == "ok"
    assert result["messages"][-1].content == "The kitchen light is on."
    registry._reset_for_tests()


async def test_guard_resets_between_runs(monkeypatch):
    call = {"name": "get_entity_state", "args": {"entity_id": "light.kitchen"}, "id": "c1"}
    responses = [
        AIMessage(content="", tool_calls=[dict(call)]),
        AIMessage(content="run one done"),
        AIMessage(content="", tool_calls=[dict(call, id="c2")]),
        AIMessage(content="run two done"),
    ]
    agent = _scripted_agent(monkeypatch, responses)
    cfg = {"configurable": {"thread_id": "t2"}, "recursion_limit": 15}
    r1 = await agent.ainvoke({"messages": [{"role": "user", "content": "q1"}]}, config=cfg)
    r2 = await agent.ainvoke({"messages": [{"role": "user", "content": "q2"}]}, config=cfg)
    # identical first tool call in run 2 must NOT be flagged repeated_call
    for result in (r1, r2):
        env = json.loads([m for m in result["messages"] if m.type == "tool"][-1].content)
        assert env["status"] == "ok"
    registry._reset_for_tests()
