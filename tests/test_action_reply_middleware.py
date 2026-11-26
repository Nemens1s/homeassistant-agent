import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.fast_path_middleware import ActionReplyMiddleware, templated_reply


def _ok(name="AI Action: Lights On"):
    return json.dumps({"status": "ok", "data": {
        "entity_id": "script.ai_action_lights_on", "name": name, "triggered": True}})


_ERR = json.dumps({"status": "error", "error": {"code": "ai_disabled", "message": "AI actions are off."}})


def _call(entity_id="script.ai_action_lights_on", call_id="call-1", params=None):
    return {"name": "trigger_action", "args": {"entity_id": entity_id, "params": params or {}},
            "id": call_id}


def _thread(text="turn on the lights", envelope=None, calls=None):
    calls = calls or [_call()]
    return [
        HumanMessage(text),
        AIMessage(content="", tool_calls=calls),
        ToolMessage(content=envelope or _ok(), tool_call_id=calls[0]["id"], name="trigger_action"),
    ]


def _request(messages, context=None):
    return SimpleNamespace(messages=messages, tools=[], runtime=SimpleNamespace(context=context))


async def _fail_handler(request):
    raise AssertionError("handler must not be called")


class _Handler:
    def __init__(self):
        self.called = False

    async def __call__(self, request):
        self.called = True
        return AIMessage("llm reply")


async def _goes_to_model(messages, context=None) -> bool:
    handler = _Handler()
    await ActionReplyMiddleware().awrap_model_call(_request(messages, context), handler)
    return handler.called


@pytest.mark.asyncio
async def test_single_ok_trigger_is_templated_without_model_call():
    resp = await ActionReplyMiddleware().awrap_model_call(_request(_thread()), _fail_handler)
    assert resp.content == "Done — triggered Lights On."
    assert resp.response_metadata == {"templated_reply": True}


@pytest.mark.asyncio
async def test_model_params_are_not_echoed():
    messages = _thread(calls=[_call(params={"target": "light.kitchen_ceiling"})])
    resp = await ActionReplyMiddleware().awrap_model_call(_request(messages), _fail_handler)
    assert resp.content == "Done — triggered Lights On."


@pytest.mark.asyncio
async def test_name_falls_back_to_entity_id():
    messages = _thread(envelope=_ok(name=""))
    resp = await ActionReplyMiddleware().awrap_model_call(_request(messages), _fail_handler)
    assert resp.content == "Done — triggered action lights on."


@pytest.mark.asyncio
async def test_trailing_full_stop_is_not_a_cue():
    messages = _thread(text="Turn on the lights.")
    resp = await ActionReplyMiddleware().awrap_model_call(_request(messages), _fail_handler)
    assert resp.content == "Done — triggered Lights On."


@pytest.mark.asyncio
async def test_two_tool_calls_go_to_model():
    calls = [_call(), _call("script.ai_action_blinds", "call-2")]
    assert await _goes_to_model(_thread(calls=calls))


@pytest.mark.asyncio
async def test_error_envelope_goes_to_model():
    assert await _goes_to_model(_thread(envelope=_ERR))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "turn on the lights and close the blinds",
    "lights on, then the fan",
    "lights on; also the fan",
    "lights on after that the fan",
    "turn on the lights. what's the temperature?",
    "lights on & blinds down",
    "lights on\nblinds down",
    "kitchen/bathroom lights on",
    "is the lights on?",
])
async def test_compound_request_goes_to_model(text):
    assert await _goes_to_model(_thread(text=text))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["turn the lights off in 10 minutes", "lights off at 22:30",
                                  "turn off the lights later"])
async def test_deferred_request_goes_to_model(text):
    assert await _goes_to_model(_thread(text=text))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["close all the blinds", "turn everything off",
                                  "close both blinds", "lights off upstairs"])
async def test_multi_target_request_without_all_param_goes_to_model(text):
    assert await _goes_to_model(_thread(text=text))


@pytest.mark.asyncio
async def test_all_request_with_all_param_is_templated():
    messages = _thread(text="close all the blinds", calls=[_call(params={"room": "all"})])
    resp = await ActionReplyMiddleware().awrap_model_call(_request(messages), _fail_handler)
    assert resp.content == "Done — triggered Lights On."


@pytest.mark.asyncio
async def test_event_run_goes_to_model():
    assert await _goes_to_model(_thread(), context={"fast_path": False})


@pytest.mark.asyncio
async def test_other_tool_goes_to_model():
    calls = [{"name": "get_entity_state", "args": {"entity_id": "light.x"}, "id": "c1"}]
    assert await _goes_to_model(_thread(calls=calls))


@pytest.mark.asyncio
async def test_first_step_goes_to_model():
    assert await _goes_to_model([HumanMessage("turn on the lights")])


def test_fast_path_templated_reply_is_marked():
    calls = [{"name": "trigger_action", "args": {"entity_id": "automation.ai_night", "params": {}},
              "id": "fastpath-1"}]
    messages = [
        HumanMessage("goodnight"),
        AIMessage(content="", tool_calls=calls, additional_kwargs={"fastpath_name": "Night"}),
        ToolMessage(content='{"status":"ok"}', tool_call_id="fastpath-1"),
    ]
    resp = templated_reply(messages)
    assert resp.content == "Done — triggered Night."
    assert resp.response_metadata == {"templated_reply": True}
