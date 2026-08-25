import json

from app.config import Settings
from app.needle.backend import Decision, FakeBackend
from app.needle.menu import Menu, MenuItem
from app.needle.router import FastPathRouter
from app.tools import registry
from app.tools.adapter import LoopGuard, to_structured_tool
from app.tools.context import ToolContext

MENU = Menu(items=(MenuItem("automation.ai_goodnight", "Goodnight"),), signature="x")


class FakeMenuProvider:
    def __init__(self, menu):
        self._menu = menu

    async def get(self):
        return self._menu


class RecordingTool:
    """Stand-in trigger tool: records the args and returns a canned envelope."""
    def __init__(self, envelope):
        self.calls = []
        self._envelope = envelope

    async def ainvoke(self, args, config=None):
        self.calls.append((args, config))
        return json.dumps(self._envelope)


def _router(backend, tool, menu=MENU, threshold=0.85):
    return FastPathRouter(backend, FakeMenuProvider(menu), tool, threshold)


async def test_confident_hit_fires_and_returns_reply():
    tool = RecordingTool({"status": "ok", "data": {"entity_id": "automation.ai_goodnight"}})
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.9)), tool)
    reply = await router.try_fast_path("goodnight", "t1")
    assert reply is not None and "automation.ai_goodnight" in reply
    assert tool.calls[0][0] == {"entity_id": "automation.ai_goodnight"}
    assert tool.calls[0][1]["configurable"]["thread_id"] == "t1"


async def test_trust_the_call_fires_on_zero_confidence():
    # Fine-tuned weights report confidence None -> 0.0. At threshold 0.0 the
    # router fires whenever a call is emitted, and still falls through on no call.
    tool = RecordingTool({"status": "ok", "data": {"entity_id": "automation.ai_goodnight"}})
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.0)), tool, threshold=0.0)
    reply = await router.try_fast_path("goodnight", "t1")
    assert reply is not None and "automation.ai_goodnight" in reply
    assert tool.calls[0][0] == {"entity_id": "automation.ai_goodnight"}


async def test_trust_the_call_falls_through_when_no_call():
    tool = RecordingTool({"status": "ok"})
    router = _router(FakeBackend(Decision(None, 0.0)), tool, threshold=0.0)
    assert await router.try_fast_path("what's the temperature", "t1") is None
    assert tool.calls == []


async def test_below_threshold_falls_through():
    tool = RecordingTool({"status": "ok"})
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.5)), tool)
    assert await router.try_fast_path("maybe goodnight", "t1") is None
    assert tool.calls == []  # tool never invoked


async def test_no_decision_falls_through():
    tool = RecordingTool({"status": "ok"})
    router = _router(FakeBackend(Decision(None, 0.99)), tool)
    assert await router.try_fast_path("what's the temperature", "t1") is None
    assert tool.calls == []


async def test_empty_menu_falls_through():
    tool = RecordingTool({"status": "ok"})
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.99)), tool,
                     menu=Menu(items=(), signature="empty"))
    assert await router.try_fast_path("goodnight", "t1") is None
    assert tool.calls == []


async def test_backend_error_falls_through():
    class Boom:
        async def classify(self, message, menu):
            raise RuntimeError("model exploded")

    tool = RecordingTool({"status": "ok"})
    router = _router(Boom(), tool)
    assert await router.try_fast_path("goodnight", "t1") is None
    assert tool.calls == []


async def test_gate_refusal_is_surfaced_not_fallen_through():
    envelope = {"status": "error",
                "error": {"code": "ai_disabled", "message": "AI-triggered actions are off."}}
    tool = RecordingTool(envelope)
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.9)), tool)
    reply = await router.try_fast_path("goodnight", "t1")
    assert reply == "AI-triggered actions are off."   # returned, not None


async def test_confident_hit_goes_through_real_gated_tool():
    """The real trigger_automation StructuredTool: gate is read, service fired."""
    class FakeRest:
        def __init__(self):
            self.calls = []
        async def get_state(self, entity_id):
            return {"entity_id": entity_id, "state": "on",   # ai_actions_switch ON
                    "attributes": {"last_triggered": "2026-08-25T22:00:00+00:00"}}
        async def call_service(self, domain, service, entity_id):
            self.calls.append((domain, service, entity_id))
            return []

    registry._reset_for_tests()
    registry.load_all(("app.tools.action.trigger_automation",))
    rest = FakeRest()
    ctx = ToolContext(settings=Settings(_env_file=None), rest=rest, ws=None)
    trigger_tool = to_structured_tool(registry.get("trigger_automation"), ctx, LoopGuard())
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.9)), trigger_tool)

    reply = await router.try_fast_path("goodnight", "t1")
    assert "automation.ai_goodnight" in reply
    assert ("automation", "trigger", "automation.ai_goodnight") in rest.calls
    registry._reset_for_tests()
