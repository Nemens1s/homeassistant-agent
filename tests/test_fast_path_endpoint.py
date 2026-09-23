"""End-to-end fast-path test: drives the real agent graph through
FastPathMiddleware with a FakeBackend hit and a scripted model that must
never run. Proves the full turn persists correctly in thread history."""

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent import factory as factory_mod
from app.config import Settings
from app.fast_path.backend import Decision, FakeBackend
from app.needle.menu import Menu, MenuItem
from app.tools import registry
from app.tools.context import ToolContext

_MENU = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="s")


class _Menu:
    async def get(self):
        return _MENU


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class FakeRest:
    async def get_state(self, entity_id):  # AI-actions switch reads on → gate passes
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    async def call_service(self, domain, service, entity_id=None, data=None):
        return []

    async def list_states(self):
        return []


async def test_fast_path_hit_persists_full_turn(monkeypatch):
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=2)  # tier-2 → trigger_action registered
    ctx = ToolContext(settings=settings, rest=FakeRest(), ws=None)
    # Scripted with an entry that must NOT be consumed (the fast path short-circuits).
    model = ScriptedModel(responses=[AIMessage(content="LLM SHOULD NOT RUN")])
    monkeypatch.setattr(factory_mod, "build_llm", lambda s: model)
    agent = factory_mod.build_agent(
        settings, ctx,
        fast_path=(FakeBackend(Decision("automation.ai_action_night", 0.9)), _Menu()),
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "goodnight"}]},
        config={"configurable": {"thread_id": "t1"}, "recursion_limit": 15},
    )
    msgs = result["messages"]
    assert isinstance(msgs[0], HumanMessage)
    assert isinstance(msgs[1], AIMessage) and msgs[1].tool_calls
    assert isinstance(msgs[2], ToolMessage)
    assert isinstance(msgs[3], AIMessage)
    assert "automation.ai_action_night" in msgs[3].content
    assert msgs[3].content != "LLM SHOULD NOT RUN"
    registry._reset_for_tests()


async def test_fast_path_decline_falls_through_to_llm(monkeypatch):
    """A DECLINING backend (confidence below threshold) must fall through to the
    scripted LLM, which should be consumed. This partially restores fallback
    coverage removed when FastPathRouter was deleted."""
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=2)
    ctx = ToolContext(settings=settings, rest=FakeRest(), ws=None)
    # No entity_id returned by the backend → falls through regardless of threshold.
    model = ScriptedModel(responses=[AIMessage(content="LLM ran the fallback")])
    monkeypatch.setattr(factory_mod, "build_llm", lambda s: model)
    agent = factory_mod.build_agent(
        settings, ctx,
        fast_path=(FakeBackend(Decision(None, 0.0)), _Menu()),
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "what time is it?"}]},
        config={"configurable": {"thread_id": "t2"}, "recursion_limit": 15},
    )
    msgs = result["messages"]
    assert msgs[-1].content == "LLM ran the fallback"
    registry._reset_for_tests()
