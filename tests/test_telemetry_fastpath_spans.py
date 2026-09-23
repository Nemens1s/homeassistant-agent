# tests/test_telemetry_fastpath_spans.py
import pytest
from types import SimpleNamespace
from langchain_core.messages import HumanMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.fast_path.backend import Decision, FakeBackend
from app.agent.middleware.fast_path_middleware import FastPathMiddleware
from app.needle.menu import Menu, MenuItem

_MENU = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="s")


class _Menu:
    async def get(self):
        return _MENU


@pytest.mark.asyncio
async def test_classify_span_emitted_on_hit():
    exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exp))
    tracer = tp.get_tracer("test")
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _Menu(), threshold=0.0, tracer=tracer)

    async def handler(request):
        raise AssertionError("no handler")

    await mw.awrap_model_call(
        SimpleNamespace(messages=[HumanMessage("goodnight")], tools=[], runtime=None),
        handler,
    )
    span = [s for s in exp.get_finished_spans() if s.name == "fast_path.classify"][0]
    assert span.attributes["gosling.fast_path.backend"] == "fake"
    assert span.attributes["gosling.fast_path.entity_id"] == "automation.ai_action_night"
    assert span.attributes["gosling.fast_path.accepted"] is True


@pytest.mark.asyncio
async def test_classify_span_miss():
    exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exp))
    tracer = tp.get_tracer("test")
    mw = FastPathMiddleware(FakeBackend(Decision(None, 0.2)),
                            _Menu(), threshold=0.5, tracer=tracer)

    called = {}

    async def handler(request):
        called["yes"] = True
        from langchain_core.messages import AIMessage
        return AIMessage("llm reply")

    await mw.awrap_model_call(
        SimpleNamespace(messages=[HumanMessage("what's the weather")], tools=[], runtime=None),
        handler,
    )
    assert called == {"yes": True}
    span = [s for s in exp.get_finished_spans() if s.name == "fast_path.classify"][0]
    assert span.attributes["gosling.fast_path.accepted"] is False


@pytest.mark.asyncio
async def test_classify_span_empty_menu_skip_reason():
    exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exp))
    tracer = tp.get_tracer("test")
    empty_menu = Menu(items=(), signature="")

    class _EmptyMenu:
        async def get(self):
            return empty_menu

    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _EmptyMenu(), threshold=0.0, tracer=tracer)

    called = {}

    async def handler(request):
        called["yes"] = True
        from langchain_core.messages import AIMessage
        return AIMessage("llm")

    await mw.awrap_model_call(
        SimpleNamespace(messages=[HumanMessage("goodnight")], tools=[], runtime=None),
        handler,
    )
    assert called == {"yes": True}
    span = [s for s in exp.get_finished_spans() if s.name == "fast_path.classify"][0]
    assert span.attributes["gosling.fast_path.skip_reason"] == "empty_menu"


@pytest.mark.asyncio
async def test_classify_span_disabled_skip_reason():
    exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exp))
    tracer = tp.get_tracer("test")
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _Menu(), threshold=0.0, tracer=tracer)

    req = SimpleNamespace(
        messages=[HumanMessage("goodnight")],
        tools=[],
        runtime=SimpleNamespace(context={"fast_path": False}),
    )

    called = {}

    async def handler(request):
        called["yes"] = True
        from langchain_core.messages import AIMessage
        return AIMessage("llm")

    await mw.awrap_model_call(req, handler)
    assert called == {"yes": True}
    spans = [s for s in exp.get_finished_spans() if s.name == "fast_path.classify"]
    # disabled before classification: a span with skip_reason=disabled should be emitted
    assert len(spans) == 1
    assert spans[0].attributes["gosling.fast_path.skip_reason"] == "disabled"


@pytest.mark.asyncio
async def test_telemetry_error_does_not_suppress_hit():
    """Fail-open: even if telemetry code raises, the fast-path hit fires."""

    class BrokenTracer:
        def start_as_current_span(self, name):
            raise RuntimeError("tracer broken")

    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _Menu(), threshold=0.0, tracer=BrokenTracer())

    async def handler(request):
        raise AssertionError("handler must not be called on a hit")

    from langchain_core.messages import AIMessage
    result = await mw.awrap_model_call(
        SimpleNamespace(messages=[HumanMessage("goodnight")], tools=[], runtime=None),
        handler,
    )
    assert isinstance(result, AIMessage)
    assert result.tool_calls[0]["args"]["entity_id"] == "automation.ai_action_night"


@pytest.mark.asyncio
async def test_no_tracer_still_works():
    """Task 2 regression: no tracer=, no store_conn= → middleware behaves as before."""
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _Menu(), threshold=0.0)

    async def handler(request):
        raise AssertionError("no handler")

    from langchain_core.messages import AIMessage
    result = await mw.awrap_model_call(
        SimpleNamespace(messages=[HumanMessage("goodnight")], tools=[], runtime=None),
        handler,
    )
    assert isinstance(result, AIMessage)
    assert result.tool_calls[0]["args"]["entity_id"] == "automation.ai_action_night"
