from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.middleware.watched_entities_middleware import WatchedEntitiesMiddleware


class FakeRest:
    def __init__(self, states, fail=()):
        self.states, self.fail, self.calls = states, set(fail), 0

    async def get_state(self, eid):
        self.calls += 1
        if eid in self.fail:
            raise RuntimeError("boom")
        return {"entity_id": eid, "state": self.states[eid],
                "attributes": {"friendly_name": eid.split(".")[1].title()}}


@dataclass
class Req:
    messages: list
    tools: list = field(default_factory=list)


async def _run(mw, req):
    seen = {}

    async def handler(r):
        seen["req"] = r
        return AIMessage(content="ok")

    await mw.awrap_model_call(req, handler)
    return seen["req"]


async def test_appends_block_to_latest_human_only():
    rest = FakeRest({"vacuum.x": "docked"})
    mw = WatchedEntitiesMiddleware(rest, ["vacuum.x"])
    old = HumanMessage("earlier")
    latest = HumanMessage("start cleaning when I leave")
    out = await _run(mw, Req([old, AIMessage("hi"), latest]))
    assert out.messages[0].content == "earlier"
    assert "<home_context>" in out.messages[-1].content
    assert 'vacuum.x "X" = docked' in out.messages[-1].content
    assert latest.content == "start cleaning when I leave"  # original untouched


async def test_over_cap_passes_through():
    ids = []
    for i in range(31):
        ids.append(f"sensor.s{i}")
    mw = WatchedEntitiesMiddleware(FakeRest({}), ids, cap=30)
    out = await _run(mw, Req([HumanMessage("hi")]))
    assert out.messages[-1].content == "hi"


async def test_failed_state_read_still_lists_entity():
    mw = WatchedEntitiesMiddleware(FakeRest({}, fail=["vacuum.x"]), ["vacuum.x"])
    out = await _run(mw, Req([HumanMessage("hi")]))
    assert "- vacuum.x\n" in out.messages[-1].content


async def test_states_cached_within_ttl():
    rest = FakeRest({"vacuum.x": "docked"})
    mw = WatchedEntitiesMiddleware(rest, ["vacuum.x"], ttl_s=60)
    await _run(mw, Req([HumanMessage("a")]))
    await _run(mw, Req([HumanMessage("b")]))
    assert rest.calls == 1


async def test_no_ids_passes_through():
    mw = WatchedEntitiesMiddleware(FakeRest({}), [])
    out = await _run(mw, Req([HumanMessage("hi")]))
    assert out.messages[-1].content == "hi"


async def test_no_human_message_passes_through():
    rest = FakeRest({"vacuum.x": "docked"})
    mw = WatchedEntitiesMiddleware(rest, ["vacuum.x"])
    out = await _run(mw, Req([AIMessage("hi")]))
    assert out.messages[-1].content == "hi"


def test_sync_wrap_passes_through_unchanged():
    rest = FakeRest({"vacuum.x": "docked"})
    mw = WatchedEntitiesMiddleware(rest, ["vacuum.x"])
    req = Req([HumanMessage("hi")])
    seen = {}

    def handler(r):
        seen["req"] = r
        return AIMessage(content="ok")

    mw.wrap_model_call(req, handler)
    assert seen["req"].messages[-1].content == "hi"


async def test_context_block_lists_watched_states():
    mw = WatchedEntitiesMiddleware(FakeRest({"vacuum.x": "docked"}), ["vacuum.x"])
    block = await mw.context_block()
    assert block.startswith("<home_context>")
    assert 'vacuum.x "X" = docked' in block


async def test_context_block_empty_when_nothing_to_attach():
    assert await WatchedEntitiesMiddleware(FakeRest({}), []).context_block() == ""
    ids = []
    for i in range(31):
        ids.append(f"sensor.s{i}")
    assert await WatchedEntitiesMiddleware(FakeRest({}), ids, cap=30).context_block() == ""
