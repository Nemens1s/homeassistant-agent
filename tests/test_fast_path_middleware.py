# tests/test_fast_path_middleware.py
import json
import pytest
from types import SimpleNamespace
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.fast_path.backend import Decision, FakeBackend
from app.fast_path.middleware import FastPathMiddleware, FASTPATH_PREFIX
from app.needle.menu import Menu, MenuItem

_MENU = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="s")

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
    assert tc["name"] == "trigger_automation"
    assert tc["args"] == {"entity_id": "automation.ai_action_night"}
    assert tc["id"].startswith(FASTPATH_PREFIX)

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
    ok = json.dumps({"status": "ok"})
    tm = ToolMessage(content=ok, tool_call_id=FASTPATH_PREFIX + "abc", name="trigger_automation")
    tm.additional_kwargs["fastpath_entity_id"] = "automation.ai_action_night"
    resp = await mw.awrap_model_call(_request([HumanMessage("x"), tm]), _fail_handler)
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
