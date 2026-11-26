import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.middleware.fast_path_middleware import ActionReplyMiddleware, FASTPATH_PREFIX

_OK = json.dumps({"status": "ok", "data": {"entity_id": "script.ai_action_lights_on"}})
_ERR = json.dumps({"status": "error", "error": {"code": "ai_disabled", "message": "AI actions are off."}})


def _call(entity_id="script.ai_action_lights_on", call_id="call-1", params=None):
    return {"name": "trigger_action", "args": {"entity_id": entity_id, "params": params or {}},
            "id": call_id}


def _thread(text="turn on the lights", envelope=_OK, calls=None, extra=None):
    calls = calls or [_call()]
    messages = [HumanMessage(text)]
    messages.extend(extra or [])
    messages.append(AIMessage(content="", tool_calls=calls))
    messages.append(ToolMessage(content=envelope, tool_call_id=calls[0]["id"], name="trigger_action"))
    return messages


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


class _Rest:
    def __init__(self, name="AI Action: Lights On", fail=False):
        self._name = name
        self._fail = fail
        self.calls = 0

    async def get_state(self, entity_id):
        self.calls += 1
        if self._fail:
            raise RuntimeError("down")
        return {"entity_id": entity_id, "attributes": {"friendly_name": self._name}}


@pytest.mark.asyncio
async def test_single_ok_trigger_is_templated_without_model_call():
    mw = ActionReplyMiddleware(_Rest())
    resp = await mw.awrap_model_call(_request(_thread()), _fail_handler)
    assert resp.content == "Done — triggered Lights On."
    assert resp.response_metadata == {"templated_reply": True}


@pytest.mark.asyncio
async def test_params_appear_in_reply():
    mw = ActionReplyMiddleware(_Rest())
    messages = _thread(calls=[_call(params={"room": "kitchen"})])
    resp = await mw.awrap_model_call(_request(messages), _fail_handler)
    assert resp.content == "Done — triggered Lights On (room: kitchen)."


@pytest.mark.asyncio
async def test_name_from_list_actions_rows_skips_rest():
    rows = {"status": "ok", "data": {"rows": [
        {"entity_id": "script.ai_action_lights_on", "name": "AI Action: Kitchen Lights"}]}}
    listing = [
        AIMessage(content="", tool_calls=[{"name": "list_actions", "args": {}, "id": "l1"}]),
        ToolMessage(content=json.dumps(rows), tool_call_id="l1", name="list_actions"),
    ]
    rest = _Rest()
    mw = ActionReplyMiddleware(rest)
    resp = await mw.awrap_model_call(_request(_thread(extra=listing)), _fail_handler)
    assert resp.content == "Done — triggered Kitchen Lights."
    assert rest.calls == 0


@pytest.mark.asyncio
async def test_name_falls_back_to_entity_id_when_lookup_fails():
    mw = ActionReplyMiddleware(_Rest(fail=True))
    resp = await mw.awrap_model_call(_request(_thread()), _fail_handler)
    assert resp.content == "Done — triggered action lights on."


@pytest.mark.asyncio
async def test_two_tool_calls_go_to_model():
    handler = _Handler()
    calls = [_call(), _call("script.ai_action_blinds", "call-2")]
    await ActionReplyMiddleware(_Rest()).awrap_model_call(_request(_thread(calls=calls)), handler)
    assert handler.called


@pytest.mark.asyncio
async def test_error_envelope_goes_to_model():
    handler = _Handler()
    await ActionReplyMiddleware(_Rest()).awrap_model_call(_request(_thread(envelope=_ERR)), handler)
    assert handler.called


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "turn on the lights and close the blinds",
    "lights on, then the fan",
    "lights on; also the fan",
    "lights on after that the fan",
])
async def test_compound_request_goes_to_model(text):
    handler = _Handler()
    await ActionReplyMiddleware(_Rest()).awrap_model_call(_request(_thread(text=text)), handler)
    assert handler.called


@pytest.mark.asyncio
async def test_event_run_goes_to_model():
    handler = _Handler()
    request = _request(_thread(), context={"fast_path": False})
    await ActionReplyMiddleware(_Rest()).awrap_model_call(request, handler)
    assert handler.called


@pytest.mark.asyncio
async def test_other_tool_goes_to_model():
    handler = _Handler()
    calls = [{"name": "get_entity_state", "args": {"entity_id": "light.x"}, "id": "c1"}]
    await ActionReplyMiddleware(_Rest()).awrap_model_call(_request(_thread(calls=calls)), handler)
    assert handler.called


@pytest.mark.asyncio
async def test_fast_path_result_is_left_to_fast_path():
    handler = _Handler()
    calls = [_call(call_id=FASTPATH_PREFIX + "abc")]
    await ActionReplyMiddleware(_Rest()).awrap_model_call(_request(_thread(calls=calls)), handler)
    assert handler.called


@pytest.mark.asyncio
async def test_first_step_goes_to_model():
    handler = _Handler()
    await ActionReplyMiddleware(_Rest()).awrap_model_call(
        _request([HumanMessage("turn on the lights")]), handler)
    assert handler.called
