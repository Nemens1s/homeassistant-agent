"""Tests for app/telemetry/middleware.py — TelemetryMiddleware spans."""
import json
import pytest
from types import SimpleNamespace
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.telemetry.middleware import TelemetryMiddleware, current_step


def _tracer_and_spans():
    exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exp))
    return tp.get_tracer("test"), exp


@pytest.mark.asyncio
async def test_model_call_records_offered_tools_and_content():
    tracer, exp = _tracer_and_spans()
    mw = TelemetryMiddleware(tracer)
    current_step.set(1)
    req = SimpleNamespace(messages=[], tools=[SimpleNamespace(name="get_weather")])

    async def handler(request):
        return AIMessage(content="sunny", response_metadata={"model_name": "qwen"})

    await mw.awrap_model_call(req, handler)
    span = exp.get_finished_spans()[0]
    assert span.attributes["gosling.tools.offered"] == '["get_weather"]'
    assert span.attributes["gosling.content.text"] == "sunny"
    assert span.attributes["gen_ai.response.model"] == "qwen"


@pytest.mark.asyncio
async def test_telemetry_exception_does_not_break_handler():
    tracer, exp = _tracer_and_spans()
    mw = TelemetryMiddleware(tracer)
    # .name missing on the tool SimpleNamespace → AttributeError inside _safe_set
    req = SimpleNamespace(messages=[], tools=[SimpleNamespace()])

    async def handler(request):
        return AIMessage(content="ok")

    resp = await mw.awrap_model_call(req, handler)
    assert resp.content == "ok"


@pytest.mark.asyncio
async def test_tool_call_parses_status():
    tracer, exp = _tracer_and_spans()
    mw = TelemetryMiddleware(tracer)
    # ToolCallRequest.tool_call is a dict with name/id/args keys
    req = SimpleNamespace(
        tool_call={"name": "trigger_automation", "id": "fastpath-1", "args": {}}
    )

    async def handler(request):
        return SimpleNamespace(
            content=json.dumps({"status": "error", "error": {"code": "ai_disabled"}})
        )

    await mw.awrap_tool_call(req, handler)
    span = [s for s in exp.get_finished_spans() if s.name.startswith("execute_tool")][0]
    assert span.attributes["gosling.tool.status"] == "error"
    assert span.attributes["gosling.tool.error_code"] == "ai_disabled"


@pytest.mark.asyncio
async def test_model_call_records_step_index():
    tracer, exp = _tracer_and_spans()
    mw = TelemetryMiddleware(tracer)
    current_step.set(3)
    req = SimpleNamespace(messages=["m1", "m2"], tools=[])

    async def handler(request):
        return AIMessage(content="done")

    await mw.awrap_model_call(req, handler)
    span = exp.get_finished_spans()[0]
    assert span.attributes["gosling.step"] == 3
    assert span.attributes["gosling.messages.count"] == 2


@pytest.mark.asyncio
async def test_tool_call_records_result_capped():
    tracer, exp = _tracer_and_spans()
    mw = TelemetryMiddleware(tracer)
    req = SimpleNamespace(
        tool_call={"name": "get_entity_state", "id": "c1", "args": {"entity_id": "light.test"}}
    )
    big_content = json.dumps({"status": "ok", "data": "x" * 70_000})

    async def handler(request):
        return SimpleNamespace(content=big_content)

    await mw.awrap_tool_call(req, handler)
    span = [s for s in exp.get_finished_spans() if s.name.startswith("execute_tool")][0]
    # Result must be capped at 64 KB (65536 bytes)
    assert len(span.attributes["gosling.tool.result"]) <= 65536
    assert span.attributes["gosling.tool.status"] == "ok"


def test_middleware_ordering_with_telemetry():
    """TelemetryMiddleware must sit immediately outside FastPathMiddleware."""
    from app.agent.middleware import build_middleware
    from app.fast_path.middleware import FastPathMiddleware
    from app.telemetry.middleware import TelemetryMiddleware
    from app.fast_path.backend import FakeBackend
    from app.tools.adapter import LoopGuard
    from app.config import Settings

    class _Menu:
        async def get(self): ...

    # Build a dummy local tracer (do NOT set the global provider)
    exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exp))
    dummy_tracer = tp.get_tracer("test-ordering")

    s = Settings(max_tier=2, enable_tool_subsetting=False)
    mws = build_middleware(
        s,
        LoopGuard(),
        "p",
        1024,
        fast_path=(FakeBackend(), _Menu()),
        telemetry_tracer=dummy_tracer,
    )
    types = [type(m) for m in mws]
    telemetry_idx = types.index(TelemetryMiddleware)
    fast_path_idx = types.index(FastPathMiddleware)
    assert telemetry_idx == fast_path_idx - 1, (
        f"TelemetryMiddleware (idx {telemetry_idx}) must be immediately before "
        f"FastPathMiddleware (idx {fast_path_idx})"
    )
