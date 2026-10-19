from dataclasses import dataclass, field
from functools import partial

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.middleware import build_middleware
from app.agent.middleware.current_time_middleware import CurrentTimeMiddleware
from app.agent.middleware.fast_path_middleware import FastPathMiddleware
from app.agent.middleware.watched_entities_middleware import WatchedEntitiesMiddleware
from app.fast_path.backend import Decision, FakeBackend
from app.needle.menu import Menu, MenuItem
from app.tools.adapter import LoopGuard
from app.config import Settings


class _Menu:
    async def get(self): ...


def _types(mws):
    return [type(m).__name__ for m in mws]


def test_fastpath_added_when_provided_and_tier2():
    s = Settings(_env_file=None, allow_actions=True, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=(FakeBackend(), _Menu()))
    assert "FastPathMiddleware" in _types(mws)
    # outermost-first: with no watched entities FastPath sits just outside
    # CurrentTime, which is always innermost
    assert isinstance(mws[-2], FastPathMiddleware)
    assert isinstance(mws[-1], CurrentTimeMiddleware)


def test_fastpath_absent_without_tier2():
    s = Settings(_env_file=None, allow_actions=False, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=(FakeBackend(), _Menu()))
    assert "FastPathMiddleware" not in _types(mws)


def test_fastpath_absent_when_none():
    s = Settings(_env_file=None, allow_actions=True, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=None)
    assert "FastPathMiddleware" not in _types(mws)


class _RecordingBackend:
    name = "recording"

    def __init__(self):
        self.messages = []

    async def classify(self, message, menu):
        self.messages.append(message)
        return Decision(None, 0.0)


class _OneItemMenu:
    async def get(self):
        return Menu(items=(MenuItem("automation.ai_action_x", "X", "x"),), signature="s")


class _Rest:
    async def get_state(self, eid):
        return {"entity_id": eid, "state": "docked", "attributes": {}}


@dataclass
class _Req:
    messages: list
    tools: list = field(default_factory=list)
    runtime: object = None


@pytest.mark.asyncio
async def test_fastpath_classifies_raw_text_without_watched_entities_block():
    # The <home_context> block and the current time are for the LLM only; with
    # them attached the classifier matches nothing and every request falls through.
    s = Settings(_env_file=None, allow_actions=True, enable_tool_subsetting=False,
                 watched_entities=["vacuum.x"])
    backend = _RecordingBackend()
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=(backend, _OneItemMenu()),
                           rest=_Rest())
    chain = []
    for m in mws:
        if isinstance(m, (FastPathMiddleware, WatchedEntitiesMiddleware, CurrentTimeMiddleware)):
            chain.append(m)
    assert len(chain) == 3

    async def model(request):
        return AIMessage(content="ok")

    # Compose outermost-first, the way create_agent nests the list.
    handler = model
    for m in reversed(chain):
        handler = partial(m.awrap_model_call, handler=handler)
    await handler(_Req(messages=[HumanMessage("turn off concorde")]))

    assert backend.messages == ["turn off concorde"]
