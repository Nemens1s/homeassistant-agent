import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.action_match_middleware import (
    ACTION_MATCH_MODEL,
    ACTION_MATCH_PREFIX,
    ActionMatchMiddleware,
    ActionRowsProvider,
)
from app.agent.middleware.telemetry_middleware import synthetic_path
from app.tools.base import ToolResult

ROWS = [
    {"entity_id": "script.ai_action_lights_on", "name": "AI Action: Lights On"},
    {"entity_id": "script.ai_action_lights_off", "name": "AI Action: Lights Off"},
    {"entity_id": "script.ai_action_stop_vacuum", "name": "AI Action: Stop Vacuum"},
    {
        "entity_id": "script.ai_action_set_brightness",
        "name": "AI Action: Set Brightness",
        "params": {
            "type": "object",
            "properties": {"percentage": {"type": "integer"}},
            "required": ["percentage"],
        },
    },
]


class _Rows:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def get(self):
        self.calls += 1
        return self.rows


class _BrokenRows:
    async def get(self):
        raise RuntimeError("HA down")


def _request(messages, context=None):
    runtime = None
    if context is not None:
        runtime = SimpleNamespace(context=context)
    return SimpleNamespace(messages=messages, tools=[], runtime=runtime)


async def _fail_handler(request):
    raise AssertionError("handler must not be called")


class _Handler:
    def __init__(self):
        self.called = False

    async def __call__(self, request):
        self.called = True
        return AIMessage("llm reply")


@pytest.mark.asyncio
async def test_exact_action_name_gives_synthetic_call_then_templated_reply():
    mw = ActionMatchMiddleware(_Rows(ROWS))
    synthetic = await mw.awrap_model_call(_request([HumanMessage("stop vacuum")]), _fail_handler)

    assert synthetic.response_metadata == {"model_name": ACTION_MATCH_MODEL, "fast_path": True}
    call = synthetic.tool_calls[0]
    assert call["name"] == "trigger_action"
    assert call["args"] == {"entity_id": "script.ai_action_stop_vacuum", "params": {}}
    assert call["id"].startswith(ACTION_MATCH_PREFIX)

    result = ToolMessage(content=json.dumps({"status": "ok"}), tool_call_id=call["id"])
    messages = [HumanMessage("stop vacuum"), synthetic, result]
    reply = await mw.awrap_model_call(_request(messages), _fail_handler)
    assert reply.content == "Done — triggered Stop Vacuum."


@pytest.mark.asyncio
async def test_polite_command_fires():
    mw = ActionMatchMiddleware(_Rows(ROWS))
    synthetic = await mw.awrap_model_call(
        _request([HumanMessage("Can you turn the lights on?")]), _fail_handler,
    )
    assert synthetic.tool_calls[0]["args"]["entity_id"] == "script.ai_action_lights_on"


@pytest.mark.asyncio
async def test_refused_action_reply_carries_the_gate_message():
    mw = ActionMatchMiddleware(_Rows(ROWS))
    synthetic = await mw.awrap_model_call(_request([HumanMessage("lights off")]), _fail_handler)
    envelope = {"status": "error", "error": {"code": "ai_disabled", "message": "AI actions are off."}}
    result = ToolMessage(content=json.dumps(envelope), tool_call_id=synthetic.tool_calls[0]["id"])
    reply = await mw.awrap_model_call(
        _request([HumanMessage("lights off"), synthetic, result]), _fail_handler,
    )
    assert reply.content == "AI actions are off."


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "lights",                      # near tie: no did-you-mean online, just fall through
    "turn on the lights and music",  # a word outside the action name
    "is the vacuum stopped?",      # a question
    "what's the temperature",      # nothing matches
])
async def test_non_winner_falls_through_to_the_handler(text):
    handler = _Handler()
    resp = await ActionMatchMiddleware(_Rows(ROWS)).awrap_model_call(
        _request([HumanMessage(text)]), handler,
    )
    assert handler.called
    assert resp.content == "llm reply"


@pytest.mark.asyncio
async def test_deferral_text_falls_through_without_listing_actions():
    rows = _Rows(ROWS)
    handler = _Handler()
    await ActionMatchMiddleware(rows).awrap_model_call(
        _request([HumanMessage("stop vacuum when I leave")]), handler,
    )
    assert handler.called
    assert rows.calls == 0


@pytest.mark.asyncio
async def test_required_params_fall_through():
    handler = _Handler()
    await ActionMatchMiddleware(_Rows(ROWS)).awrap_model_call(
        _request([HumanMessage("set brightness")]), handler,
    )
    assert handler.called


@pytest.mark.asyncio
async def test_event_runs_skip_it():
    rows = _Rows(ROWS)
    handler = _Handler()
    await ActionMatchMiddleware(rows).awrap_model_call(
        _request([HumanMessage("stop vacuum")], context={"fast_path": False}), handler,
    )
    assert handler.called
    assert rows.calls == 0


@pytest.mark.asyncio
async def test_listing_failure_falls_through():
    handler = _Handler()
    await ActionMatchMiddleware(_BrokenRows()).awrap_model_call(
        _request([HumanMessage("stop vacuum")]), handler,
    )
    assert handler.called


@pytest.mark.asyncio
async def test_fast_path_tool_results_are_left_alone():
    handler = _Handler()
    result = ToolMessage(content=json.dumps({"status": "ok"}), tool_call_id="fastpath-abc")
    await ActionMatchMiddleware(_Rows(ROWS)).awrap_model_call(
        _request([HumanMessage("x"), AIMessage(""), result]), handler,
    )
    assert handler.called


@pytest.mark.asyncio
async def test_rows_provider_caches_and_handles_errors():
    calls = []

    async def fetch():
        calls.append(1)
        return ToolResult.ok({"rows": ROWS[:1], "total": 1})

    provider = ActionRowsProvider(fetch, ttl_s=60)
    assert await provider.get() == ROWS[:1]
    assert await provider.get() == ROWS[:1]
    assert len(calls) == 1

    async def failing():
        return ToolResult.error("ha_unreachable", "down")

    assert await ActionRowsProvider(failing).get() == []


class _Span:
    def __init__(self):
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Tracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name):
        span = _Span()
        self.spans.append((name, span))
        return span


@pytest.mark.asyncio
async def test_hit_records_a_classify_span():
    tracer = _Tracer()
    mw = ActionMatchMiddleware(_Rows(ROWS), tracer=tracer)
    await mw.awrap_model_call(_request([HumanMessage("stop vacuum")]), _fail_handler)
    name, span = tracer.spans[0]
    assert name == "fast_path.classify"
    assert span.attributes["gosling.fast_path.backend"] == "action_match"
    assert span.attributes["gosling.fast_path.entity_id"] == "script.ai_action_stop_vacuum"
    assert span.attributes["gosling.fast_path.accepted"] is True


def test_synthetic_path_names_the_tier():
    assert synthetic_path(ACTION_MATCH_MODEL) == "action_match"
    assert synthetic_path("needle-remote") == "fast_path"
