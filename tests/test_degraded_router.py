import httpx
import pytest

from app.config import Settings
from app.degraded.router import DEFERRAL_REPLY, CommandRouter
from app.fast_path.backend import Decision
from app.health import ServiceHealth
from app.needle.menu import Menu, MenuItem
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {"entity_id": "script.ai_action_lights_on", "state": "off",
     "attributes": {"friendly_name": "AI Action: Living Room Lights On"}},
    {"entity_id": "script.ai_action_lights_off", "state": "off",
     "attributes": {"friendly_name": "AI Action: Living Room Lights Off"}},
]


class FakeRest:
    def __init__(self):
        self.calls = []

    async def list_states(self):
        return STATES

    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    async def get_script_config(self, object_id):
        return {}

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service))
        return []


@pytest.fixture(autouse=True)
def tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.list_actions", "app.tools.action.trigger_action"))
    yield
    registry._reset_for_tests()


async def _never():
    return False


def _health(down: bool):
    health = ServiceHealth("llm", _never)
    if down:
        health.mark_down("refused")
    return health


def _router(rest, llm_down=True, fast_path=None, fp_health=None):
    settings = Settings(_env_file=None, allow_actions=True)
    ctx = ToolContext(settings=settings, rest=rest, ws=None)
    return CommandRouter(ctx, None, _health(llm_down), fast_path=fast_path, fp_health=fp_health)


def test_is_command():
    assert CommandRouter.is_command("  /help")
    assert not CommandRouter.is_command("help")


async def test_slash_command_handled_even_when_llm_up():
    reply = await _router(FakeRest(), llm_down=False).handle("/help", "t1")
    assert reply.kind == "command"
    assert "/actions" in reply.text


async def test_unknown_command_shows_help():
    reply = await _router(FakeRest()).handle("/frobnicate", "t1")
    assert reply.text.startswith("Commands:")


async def test_free_text_passes_through_when_llm_up():
    assert await _router(FakeRest(), llm_down=False).handle("lights on", "t1") is None


async def test_fallback_runs_clear_match():
    rest = FakeRest()
    reply = await _router(rest).handle("turn on the living room lights", "t1")
    assert reply.kind == "degraded"
    assert reply.text == "Done — triggered Living Room Lights On."
    assert rest.calls == [("script", "ai_action_lights_on")]
    names = []
    for call in reply.tool_calls:
        names.append(call["name"])
    assert names == ["list_actions", "trigger_action"]


async def test_fallback_lists_candidates_on_near_tie():
    rest = FakeRest()
    reply = await _router(rest).handle("living room lights", "t1")
    assert reply.text.startswith("Did you mean:")
    assert rest.calls == []


async def test_fallback_refuses_deferred_request():
    rest = FakeRest()
    reply = await _router(rest).handle("when the door opens turn on the living room lights", "t1")
    assert reply.text == DEFERRAL_REPLY
    assert rest.calls == []


async def test_fallback_offline_message_when_nothing_matches():
    reply = await _router(FakeRest()).handle("what's the temperature?", "t1")
    assert "language model is offline" in reply.text
    assert "/actions" in reply.text


class _Menu:
    async def get(self):
        return Menu(items=(MenuItem("script.ai_action_lights_off", "Lights Off", "off"),), signature="s")


class _Backend:
    name = "fake"

    def __init__(self, decision=None, error=None):
        self.decision = decision
        self.error = error
        self.calls = 0

    async def classify(self, message, menu):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.decision


async def test_fallback_uses_fast_path_first():
    rest = FakeRest()
    backend = _Backend(Decision("script.ai_action_lights_off", 0.9))
    reply = await _router(rest, fast_path=(backend, _Menu())).handle("darkness please", "t1")
    assert reply.text == "Done — triggered Lights Off."
    assert rest.calls == [("script", "ai_action_lights_off")]


async def test_fallback_skips_fast_path_while_down():
    backend = _Backend(Decision("script.ai_action_lights_off", 0.9))
    fp_health = _health(down=True)
    reply = await _router(FakeRest(), fast_path=(backend, _Menu()), fp_health=fp_health).handle(
        "darkness please", "t1")
    assert backend.calls == 0
    assert "language model is offline" in reply.text


async def test_fast_path_connection_error_marks_it_down_and_continues():
    backend = _Backend(error=httpx.ConnectError("refused"))
    fp_health = _health(down=False)
    reply = await _router(FakeRest(), fast_path=(backend, _Menu()), fp_health=fp_health).handle(
        "turn on the living room lights", "t1")
    assert fp_health.is_down
    assert reply.text == "Done — triggered Living Room Lights On."
