"""Per-thread history cap: history_removals boundary logic + end-to-end prune."""

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from app.agent import factory as factory_mod
from app.agent.middleware.history_cap_middleware import history_removals
from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


def _msgs(*specs) -> list:
    """Build a message list with stable ids from (kind, text) specs."""
    out = []
    for i, (kind, text) in enumerate(specs):
        mid = f"m{i}"
        if kind == "human":
            out.append(HumanMessage(content=text, id=mid))
        elif kind == "ai":
            out.append(AIMessage(content=text, id=mid))
        elif kind == "tool":
            out.append(ToolMessage(content=text, tool_call_id="tc", id=mid))
    return out


def _removed_ids(messages, cap) -> set:
    removals = history_removals(messages, cap)
    for r in removals:
        assert isinstance(r, RemoveMessage)
    return {r.id for r in removals}


def test_disabled_when_cap_zero_or_within_limit():
    msgs = _msgs(("human", "q1"), ("ai", "a1"), ("human", "q2"), ("ai", "a2"))
    assert history_removals(msgs, 0) == []          # disabled
    assert history_removals(msgs, 4) == []          # exactly at cap
    assert history_removals(msgs, 99) == []          # under cap


def test_prune_keeps_last_turn_and_drops_older():
    msgs = _msgs(("human", "q1"), ("ai", "a1"), ("human", "q2"), ("ai", "a2"))
    removed = _removed_ids(msgs, 2)
    # keeps the last human-started turn (q2, a2); drops the first
    assert removed == {"m0", "m1"}


def test_prune_never_orphans_a_tool_message():
    # A tool group: human, ai(tool call), tool result, ai(final), then next turn.
    msgs = _msgs(
        ("human", "q1"), ("ai", "call"), ("tool", "result"), ("ai", "a1"),
        ("human", "q2"), ("ai", "a2"),
    )
    # Cap of 4 would naively cut mid-group; the human-boundary snap must not
    # leave the tool result without its preceding human turn.
    removed = _removed_ids(msgs, 4)
    kept = [m for m in msgs if m.id not in removed]
    # No kept ToolMessage may appear before a HumanMessage in the kept window.
    kinds = [m.type for m in kept]
    if "tool" in kinds:
        assert kinds.index("human") < kinds.index("tool")
    # And the kept window starts on a human turn.
    assert kept[0].type == "human"


# ---------- end-to-end: prune actually shrinks persisted state ----------

class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class FakeRest:
    async def list_states(self):
        return []


def _capped_agent(monkeypatch, responses, cap):
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=1, max_history_messages=cap,
                        enable_tool_subsetting=False)
    ctx = ToolContext(settings=settings, rest=FakeRest(), ws=None)
    monkeypatch.setattr(factory_mod, "build_llm", lambda s: ScriptedModel(responses=responses))
    return factory_mod.build_agent(settings, ctx)


async def test_history_capped_across_turns(monkeypatch):
    # Plain (no-tool) replies: each turn adds Human + AI = 2 messages.
    responses = [AIMessage(content=f"a{i}") for i in range(1, 5)]
    agent = _capped_agent(monkeypatch, responses, cap=3)
    cfg = {"configurable": {"thread_id": "cap"}, "recursion_limit": 10}

    for i in range(1, 5):
        await agent.ainvoke({"messages": [{"role": "user", "content": f"q{i}"}]}, config=cfg)

    state = await agent.aget_state(cfg)
    messages = state.values["messages"]
    assert len(messages) <= 3                     # bounded, not 8
    assert messages[0].type == "human"            # window starts on a human turn
    contents = [m.content for m in messages]
    assert "q1" not in contents                    # oldest turn pruned
    assert messages[-1].content == "a4"            # latest answer retained
    registry._reset_for_tests()
