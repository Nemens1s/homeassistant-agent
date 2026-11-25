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
from app.needle.menu import Menu, MenuItem

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
async def test_question_falls_through_without_listing_actions():
    rows = _Rows(ROWS)
    handler = _Handler()
    await ActionMatchMiddleware(rows).awrap_model_call(
        _request([HumanMessage("is the vacuum stopped?")]), handler,
    )
    assert handler.called
    assert rows.calls == 0


@pytest.mark.asyncio
async def test_optional_params_fall_through():
    # An optional room is the fast path's to fill from the request area.
    rows = [{"entity_id": "script.ai_action_lights_on", "name": "AI Action: Lights On",
             "params": {"type": "object", "properties": {"room": {"type": "string"}}}}]
    handler = _Handler()
    await ActionMatchMiddleware(_Rows(rows)).awrap_model_call(
        _request([HumanMessage("lights on")]), handler,
    )
    assert handler.called


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
async def test_fast_path_tool_results_are_left_alone():
    handler = _Handler()
    result = ToolMessage(content=json.dumps({"status": "ok"}), tool_call_id="fastpath-abc")
    await ActionMatchMiddleware(_Rows(ROWS)).awrap_model_call(
        _request([HumanMessage("x"), AIMessage(""), result]), handler,
    )
    assert handler.called


class _MenuProvider:
    def __init__(self, items=(), fail=False):
        self.items = tuple(items)
        self.fail = fail
        self.calls = 0

    async def get(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("HA down")
        return Menu(items=self.items, signature="s")


@pytest.mark.asyncio
async def test_rows_provider_turns_menu_items_into_sorted_rows():
    menu = _MenuProvider([
        MenuItem("script.ai_action_stop_vacuum", "AI Action: Stop Vacuum"),
        MenuItem("script.ai_action_lights_on", "AI Action: Lights On", parameters={"type": "object"}),
    ])
    rows = await ActionRowsProvider(menu).get()
    assert rows == [
        {"entity_id": "script.ai_action_lights_on", "name": "AI Action: Lights On",
         "params": {"type": "object"}},
        {"entity_id": "script.ai_action_stop_vacuum", "name": "AI Action: Stop Vacuum",
         "params": {}},
    ]


@pytest.mark.asyncio
async def test_rows_provider_backs_off_after_a_failure():
    menu = _MenuProvider(fail=True)
    provider = ActionRowsProvider(menu, backoff_s=60)
    assert await provider.get() == []
    assert await provider.get() == []
    assert menu.calls == 1


@pytest.mark.asyncio
async def test_listing_failure_falls_through():
    handler = _Handler()
    mw = ActionMatchMiddleware(ActionRowsProvider(_MenuProvider(fail=True)))
    await mw.awrap_model_call(_request([HumanMessage("stop vacuum")]), handler)
    assert handler.called


def test_synthetic_path_names_the_tier():
    assert synthetic_path(ACTION_MATCH_MODEL) == "action_match"
    assert synthetic_path("needle-remote") == "fast_path"
