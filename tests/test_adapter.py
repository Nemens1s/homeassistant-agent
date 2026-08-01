import json

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.tools.adapter import LoopGuard, build_tools, to_structured_tool
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools import registry
from app.tools.context import ToolContext


class _Params(BaseModel):
    entity_id: str = Field(description="entity id")


class _FakeRest:
    async def list_states(self):
        return [{"entity_id": "light.kitchen"}, {"entity_id": "light.bedroom"}]


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=_FakeRest(), ws=None)


def _make_tool(handler, name="demo"):
    defn = ToolDefinition(
        name=name, description="demo tool", params_model=_Params,
        tier=Tier.READ, handler=handler,
    )
    return to_structured_tool(defn, _ctx(), LoopGuard())


async def test_ok_result_passthrough():
    async def handler(params, ctx):
        return ToolResult.ok({"state": "on"})

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out == {"status": "ok", "data": {"state": "on"}}


async def test_404_maps_to_entity_not_found_with_suggestions():
    async def handler(params, ctx):
        raise httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(404),
        )

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitche"}))
    assert out["status"] == "error"
    assert out["error"]["code"] == "entity_not_found"
    assert "light.kitchen" in out["data"]["did_you_mean"]


async def test_connect_error_maps_to_ha_unreachable():
    async def handler(params, ctx):
        raise httpx.ConnectError("refused")

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out["error"]["code"] == "ha_unreachable"


async def test_timeout_maps_to_ha_timeout():
    async def handler(params, ctx):
        raise TimeoutError()

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out["error"]["code"] == "ha_timeout"


async def test_loop_guard_blocks_identical_consecutive_call():
    async def handler(params, ctx):
        return ToolResult.ok("fine")

    tool = _make_tool(handler)
    first = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    second = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert first["status"] == "ok"
    assert second["error"]["code"] == "repeated_call"
    third = json.loads(await tool.ainvoke({"entity_id": "light.bedroom"}))
    assert third["status"] == "ok"


async def test_audit_log_line(caplog):
    async def handler(params, ctx):
        return ToolResult.ok("x")

    tool = _make_tool(handler)
    with caplog.at_level("INFO", logger="agent.tools"):
        await tool.ainvoke({"entity_id": "light.kitchen"})
    line = caplog.records[-1].getMessage()
    assert "tool=demo" in line and "status=ok" in line and "duration_ms=" in line


async def test_runtime_error_maps_to_ha_error():
    async def handler(params, ctx):
        raise RuntimeError("unknown command")

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out["status"] == "error"
    assert out["error"]["code"] == "ha_error"


async def test_unexpected_exception_maps_to_internal_error():
    async def handler(params, ctx):
        raise KeyError("entity_id")

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out["status"] == "error"
    assert out["error"]["code"] == "internal_error"


async def test_loop_guard_reset_clears_repeated_call_block():
    async def handler(params, ctx):
        return ToolResult.ok("fine")

    guard = LoopGuard()
    defn = ToolDefinition(
        name="demo", description="demo tool", params_model=_Params,
        tier=Tier.READ, handler=handler,
    )
    tool = to_structured_tool(defn, _ctx(), guard)
    await tool.ainvoke({"entity_id": "light.kitchen"})
    second = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert second["error"]["code"] == "repeated_call"
    guard.reset()
    after_reset = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert after_reset["status"] == "ok"


async def test_loop_guard_is_per_thread():
    async def handler(params, ctx):
        return ToolResult.ok("fine")

    guard = LoopGuard()
    defn = ToolDefinition(
        name="demo2", description="d", params_model=_Params,
        tier=Tier.READ, handler=handler,
    )
    tool = to_structured_tool(defn, _ctx(), guard)
    cfg_a = {"configurable": {"thread_id": "a"}}
    cfg_b = {"configurable": {"thread_id": "b"}}
    first = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}, config=cfg_a))
    # identical call on a DIFFERENT thread must not be flagged
    other = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}, config=cfg_b))
    repeat = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}, config=cfg_a))
    assert first["status"] == "ok"
    assert other["status"] == "ok"
    assert repeat["error"]["code"] == "repeated_call"


async def test_permission_error_maps_to_domain_not_allowed():
    async def handler(params, ctx):
        raise PermissionError("write domain not allowed: 'lock'")

    tool = _make_tool(handler, name="demo3")
    out = json.loads(await tool.ainvoke({"entity_id": "lock.front"}))
    assert out["error"]["code"] == "domain_not_allowed"


def test_build_tools_filters_by_tier():
    registry._reset_for_tests()

    async def handler(params, ctx):
        return ToolResult.ok(None)

    registry.register(ToolDefinition("read_one", "d", _Params, Tier.READ, handler))
    registry.register(ToolDefinition("act_one", "d", _Params, Tier.ACTION, handler))
    tools = build_tools(_ctx(), max_tier=1)
    assert [t.name for t in tools] == ["read_one"]
    registry._reset_for_tests()


class RecordingSink:
    def __init__(self):
        self.rows = []

    async def record(self, **fields):
        self.rows.append(fields)


def _action_tool(handler, sink, name="act_demo"):
    from app.config import Settings
    ctx = ToolContext(settings=Settings(_env_file=None), rest=_FakeRest(), ws=None)
    ctx.audit = sink
    defn = ToolDefinition(name=name, description="d", params_model=_Params,
                          tier=Tier.ACTION, handler=handler)
    return to_structured_tool(defn, ctx, LoopGuard())


async def test_audit_line_contains_tier(caplog):
    async def handler(params, ctx):
        return ToolResult.ok("x")

    tool = _make_tool(handler, name="tiered")
    with caplog.at_level("INFO", logger="agent.tools"):
        await tool.ainvoke({"entity_id": "light.kitchen"})
    assert "tier=1" in caplog.records[-1].getMessage()


async def test_tier2_records_to_sink_and_actions_logger(caplog):
    async def handler(params, ctx):
        return ToolResult.ok("done")

    sink = RecordingSink()
    tool = _action_tool(handler, sink)
    with caplog.at_level("INFO", logger="agent.actions"):
        await tool.ainvoke({"entity_id": "light.kitchen"},
                           config={"configurable": {"thread_id": "t9"}})
    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["thread_id"] == "t9"
    assert row["entity_id"] == "light.kitchen"
    assert row["domain"] == "light"
    assert row["status"] == "ok"
    assert any(r.name == "agent.actions" for r in caplog.records)


async def test_tier1_never_touches_sink():
    async def handler(params, ctx):
        return ToolResult.ok("x")

    sink = RecordingSink()
    from app.config import Settings
    ctx = ToolContext(settings=Settings(_env_file=None), rest=_FakeRest(), ws=None)
    ctx.audit = sink
    defn = ToolDefinition(name="read_demo", description="d", params_model=_Params,
                          tier=Tier.READ, handler=handler)
    tool = to_structured_tool(defn, ctx, LoopGuard())
    await tool.ainvoke({"entity_id": "light.kitchen"})
    assert sink.rows == []


async def test_tier2_refused_outcome_is_audited():
    async def handler(params, ctx):
        return ToolResult.error("domain_not_allowed", "nope")

    sink = RecordingSink()
    tool = _action_tool(handler, sink)
    await tool.ainvoke({"entity_id": "lock.front"})
    assert sink.rows[0]["status"] == "error"
    assert sink.rows[0]["error_code"] == "domain_not_allowed"
