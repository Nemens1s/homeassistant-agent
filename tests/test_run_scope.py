import json

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app.agent import factory as factory_mod
from app.agent.run_scope import (
    LANGUAGE_KEY,
    SUPPRESS_NOTIFY_KEY,
    RunScope,
    current_scope,
    scope_configurable,
    scope_from_config,
    use_scope,
)
from app.config import Settings
from app.tools import registry
from app.tools.adapter import LoopGuard, to_structured_tool
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.context import ToolContext


class _Empty(BaseModel):
    pass


def _probe(seen):
    async def handler(params, ctx):
        seen.append(current_scope())
        return ToolResult.ok({})

    return ToolDefinition(
        name="probe", description="probe", params_model=_Empty, tier=Tier.READ, handler=handler
    )


def test_defaults_without_keys():
    assert scope_from_config(None) == RunScope()
    assert scope_from_config({"configurable": {"thread_id": "t1"}}) == RunScope(thread_id="t1")


def test_reads_keys():
    config = {"configurable": {"thread_id": "events", **scope_configurable("ru", True)}}
    assert scope_from_config(config) == RunScope(language="ru", suppress_notify=True, thread_id="events")


def test_use_scope_sets_and_restores():
    assert current_scope() == RunScope()
    with use_scope(RunScope(language="et")):
        assert current_scope().language == "et"
    assert current_scope() == RunScope()


async def test_adapter_exposes_scope_to_handler():
    seen = []
    ctx = ToolContext(settings=Settings(_env_file=None), rest=None, ws=None)
    tool = to_structured_tool(_probe(seen), ctx, LoopGuard())
    config = {"configurable": {"thread_id": "t5", LANGUAGE_KEY: "ru", SUPPRESS_NOTIFY_KEY: True}}
    out = json.loads(await tool.ainvoke({}, config=config))
    assert out["status"] == "ok"
    assert seen == [RunScope(language="ru", suppress_notify=True, thread_id="t5")]
    assert current_scope() == RunScope()  # reset after the call


class _ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


async def test_scope_reaches_handlers_inside_the_graph(monkeypatch):
    seen = []
    registry._reset_for_tests()
    registry.register(_probe(seen))
    monkeypatch.setattr(registry, "load_all", lambda *a, **k: None)
    settings = Settings(_env_file=None, max_tier=1, enable_tool_subsetting=False)
    ctx = ToolContext(settings=settings, rest=None, ws=None)
    model = _ScriptedModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "probe", "args": {}, "id": "c1"}]),
        AIMessage(content="done"),
    ])
    monkeypatch.setattr(factory_mod, "build_llm", lambda s: model)
    agent = factory_mod.build_agent(settings, ctx)
    config = {
        "configurable": {"thread_id": "events", **scope_configurable("ru", True)},
        "recursion_limit": 15,
    }
    await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]}, config=config)
    registry._reset_for_tests()
    assert seen == [RunScope(language="ru", suppress_notify=True, thread_id="events")]
