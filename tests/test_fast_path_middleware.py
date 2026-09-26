# tests/test_fast_path_middleware.py
import json
import pytest
from types import SimpleNamespace
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.fast_path.backend import Decision, FakeBackend
from app.agent.middleware.fast_path_middleware import FastPathMiddleware, FASTPATH_PREFIX
from app.needle.menu import Menu, MenuItem

_MENU = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="s")

_MENU_WITH_REQUIRED = Menu(
    items=(
        MenuItem(
            "script.ai_action_set_brightness",
            "Set Brightness",
            "Set room brightness",
            parameters={
                "type": "object",
                "properties": {
                    "room": {"type": "string", "enum": ["living_room"]},
                    "percentage": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["room", "percentage"],
            },
        ),
    ),
    signature="s2",
)

class _FakeMenu:
    def __init__(self, menu): self._m = menu
    async def get(self): return self._m

def _request(messages, tools=None):
    return SimpleNamespace(messages=messages, tools=tools or [], runtime=None)

async def _fail_handler(request):
    raise AssertionError("handler must not be called")

@pytest.mark.asyncio
async def test_hit_returns_synthetic_tool_call():
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _FakeMenu(_MENU), threshold=0.0)
    resp = await mw.awrap_model_call(_request([HumanMessage("goodnight")]), _fail_handler)
    assert isinstance(resp, AIMessage)
    assert resp.response_metadata == {"model_name": "fake", "fast_path": True}
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc["name"] == "trigger_action"
    assert tc["args"] == {"entity_id": "automation.ai_action_night", "params": {}}
    assert tc["id"].startswith(FASTPATH_PREFIX)

@pytest.mark.asyncio
async def test_synthetic_call_includes_params():
    from app.agent.middleware.fast_path_middleware import FastPathMiddleware

    class _Backend:
        name = "fake"

    mw = FastPathMiddleware(backend=_Backend(), menu_provider=None, threshold=0.0)
    msg = mw._synthetic_call("script.ai_action_lights_on", {"room": "living_room"})
    call = msg.tool_calls[0]
    assert call["name"] == "trigger_action"
    assert call["args"] == {"entity_id": "script.ai_action_lights_on",
                            "params": {"room": "living_room"}}

@pytest.mark.asyncio
async def test_miss_calls_handler():
    mw = FastPathMiddleware(FakeBackend(Decision(None, 0.0)), _FakeMenu(_MENU), threshold=0.5)
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm reply")
    resp = await mw.awrap_model_call(_request([HumanMessage("what's the weather")]), handler)
    assert called == {"yes": True}
    assert resp.content == "llm reply"

@pytest.mark.asyncio
async def test_step_after_fastpath_tool_returns_templated_reply():
    mw = FastPathMiddleware(FakeBackend(), _FakeMenu(_MENU), threshold=0.0)
    call_id = FASTPATH_PREFIX + "abc"
    ok = json.dumps({"status": "ok"})
    # The entity_id is now read from the preceding AIMessage's tool_calls args,
    # not from ToolMessage.additional_kwargs (which LangGraph does not propagate).
    ai_msg = AIMessage(
        content="",
        tool_calls=[{"name": "trigger_action",
                     "args": {"entity_id": "automation.ai_action_night", "params": {}},
                     "id": call_id}],
    )
    tm = ToolMessage(content=ok, tool_call_id=call_id, name="trigger_action")
    resp = await mw.awrap_model_call(_request([HumanMessage("x"), ai_msg, tm]), _fail_handler)
    assert isinstance(resp, AIMessage)
    assert "automation.ai_action_night" in resp.content

@pytest.mark.asyncio
async def test_mid_loop_tool_message_falls_through():
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _FakeMenu(_MENU), threshold=0.0)
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("continue")
    tm = ToolMessage(content="{}", tool_call_id="call_normal_123", name="get_weather")
    await mw.awrap_model_call(_request([HumanMessage("x"), AIMessage("t"), tm]), handler)
    assert called == {"yes": True}

@pytest.mark.asyncio
async def test_empty_menu_falls_through():
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _FakeMenu(Menu(items=(), signature="")), threshold=0.0)
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm")
    await mw.awrap_model_call(_request([HumanMessage("goodnight")]), handler)
    assert called == {"yes": True}

@pytest.mark.asyncio
async def test_backend_error_falls_through():
    class Boom:
        name = "boom"
        async def classify(self, message, menu): raise RuntimeError("down")
    mw = FastPathMiddleware(Boom(), _FakeMenu(_MENU), threshold=0.0)
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm")
    await mw.awrap_model_call(_request([HumanMessage("goodnight")]), handler)
    assert called == {"yes": True}

@pytest.mark.asyncio
async def test_opt_out_flag_falls_through():
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _FakeMenu(_MENU), threshold=0.0)
    req = SimpleNamespace(messages=[HumanMessage("goodnight")], tools=[],
                          runtime=SimpleNamespace(context={"fast_path": False}))
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm")
    await mw.awrap_model_call(req, handler)
    assert called == {"yes": True}


@pytest.mark.asyncio
async def test_params_included_in_reply_text():
    """Params from the synthetic call appear in the plain-text reply so the LLM can see them."""
    mw = FastPathMiddleware(FakeBackend(), _FakeMenu(_MENU), threshold=0.0)
    call_id = FASTPATH_PREFIX + "abc"
    ok = json.dumps({"status": "ok"})
    ai_msg = AIMessage(
        content="",
        tool_calls=[{"name": "trigger_action",
                     "args": {"entity_id": "script.ai_action_set_brightness",
                              "params": {"room": "living_room", "percentage": 20}},
                     "id": call_id}],
    )
    tm = ToolMessage(content=ok, tool_call_id=call_id, name="trigger_action")
    resp = await mw.awrap_model_call(_request([HumanMessage("x"), ai_msg, tm]), _fail_handler)
    assert "room: living_room" in resp.content
    assert "percentage: 20" in resp.content


@pytest.mark.asyncio
async def test_missing_required_params_falls_through_to_llm():
    """When the fast-path decision lacks required params the LLM must handle it."""
    decision = Decision("script.ai_action_set_brightness", 0.95, arguments={})
    mw = FastPathMiddleware(
        FakeBackend(decision),
        _FakeMenu(_MENU_WITH_REQUIRED),
        threshold=0.0,
    )
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm reply")
    resp = await mw.awrap_model_call(
        _request([HumanMessage("make lights a bit less bright")]), handler
    )
    assert called == {"yes": True}
    assert resp.content == "llm reply"


@pytest.mark.asyncio
async def test_all_required_params_present_triggers():
    """When the decision supplies all required params the fast path still fires."""
    decision = Decision(
        "script.ai_action_set_brightness", 0.95,
        arguments={"room": "living_room", "percentage": 20},
    )
    mw = FastPathMiddleware(
        FakeBackend(decision),
        _FakeMenu(_MENU_WITH_REQUIRED),
        threshold=0.0,
    )
    resp = await mw.awrap_model_call(
        _request([HumanMessage("set brightness to 20 in living room")]), _fail_handler
    )
    assert isinstance(resp, AIMessage)
    tc = resp.tool_calls[0]
    assert tc["args"]["params"] == {"room": "living_room", "percentage": 20}


@pytest.mark.asyncio
async def test_no_required_field_in_schema_triggers_without_args():
    """A script with no required params still triggers even if arguments is empty."""
    menu = Menu(
        items=(
            MenuItem(
                "script.ai_action_lights_on",
                "Lights On",
                "Turn lights on",
                parameters={"type": "object", "properties": {}},
            ),
        ),
        signature="s3",
    )
    decision = Decision("script.ai_action_lights_on", 0.9, arguments={})
    mw = FastPathMiddleware(FakeBackend(decision), _FakeMenu(menu), threshold=0.0)
    resp = await mw.awrap_model_call(_request([HumanMessage("lights on")]), _fail_handler)
    assert isinstance(resp, AIMessage)
    assert resp.tool_calls[0]["args"]["entity_id"] == "script.ai_action_lights_on"
