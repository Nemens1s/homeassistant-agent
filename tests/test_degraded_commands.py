import pytest

from app.config import Settings
from app.degraded.commands import (
    cmd_actions,
    cmd_cancel,
    cmd_help,
    cmd_notes,
    cmd_run,
    parse_run_args,
)
from app.degraded.tools import ToolRunner
from app.health import ServiceHealth
from app.memory.store import NoteStore
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {"entity_id": "script.ai_action_lights_on", "state": "off",
     "attributes": {"friendly_name": "AI Action: Living Room Lights On"}},
    {"entity_id": "script.ai_action_lights_off", "state": "off",
     "attributes": {"friendly_name": "AI Action: Living Room Lights Off"}},
    {"entity_id": "script.ai_action_dim", "state": "off",
     "attributes": {"friendly_name": "AI Action: Dim"}},
    {"entity_id": "automation.ai_goodnight", "state": "on",
     "attributes": {"friendly_name": "Goodnight"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
]

DIM_FIELDS = {"fields": {"level": {"required": True, "selector": {"number": {"min": 1, "max": 100}}}}}


class FakeRest:
    def __init__(self, switch="on"):
        self.switch = switch
        self.calls = []

    async def list_states(self):
        return STATES

    async def get_state(self, entity_id):
        if entity_id == "input_boolean.ai_triggered_actions":
            return {"entity_id": entity_id, "state": self.switch, "attributes": {}}
        return {"entity_id": entity_id, "state": "off", "attributes": {}}

    async def get_script_config(self, object_id):
        if object_id == "ai_action_dim":
            return DIM_FIELDS
        return {}

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id, data))
        return []


@pytest.fixture(autouse=True)
def tools():
    registry._reset_for_tests()
    registry.load_all((
        "app.tools.read.list_actions",
        "app.tools.action.trigger_action",
        "app.tools.memory.list_scheduled",
        "app.tools.memory.cancel_scheduled",
    ))
    yield
    registry._reset_for_tests()


@pytest.fixture
def store():
    s = NoteStore("")
    yield s
    s.close()


def _runner(rest, store=None, allow_actions=True):
    settings = Settings(_env_file=None, allow_actions=allow_actions)
    ctx = ToolContext(settings=settings, rest=rest, ws=None, notes=store)
    return ToolRunner(ctx, None, "t1")


def test_parse_run_args():
    assert parse_run_args("Dim level=30") == ("Dim", {"level": 30})
    assert parse_run_args("2") == ("2", {})
    assert parse_run_args("Set mode mode=eco") == ("Set mode", {"mode": "eco"})
    assert parse_run_args("") == ("", {})


async def test_help_lists_commands_and_llm_status():
    async def probe():
        return False

    health = ServiceHealth("llm", probe)
    health.mark_down("refused")
    text = await cmd_help("", _runner(FakeRest()), health)
    assert "/actions" in text and "/run" in text and "/notes" in text and "/cancel" in text
    assert "LLM: offline since" in text


async def test_actions_lists_sorted_numbered_names():
    text = await cmd_actions("", _runner(FakeRest()))
    assert "1. Dim" in text
    assert "2. Goodnight" in text
    assert "3. Living Room Lights Off" in text
    assert "light.kitchen" not in text


async def test_run_by_number():
    rest = FakeRest()
    text = await cmd_run("2", _runner(rest))
    assert text == "Done — triggered Goodnight."
    assert rest.calls[0][:3] == ("automation", "trigger", "automation.ai_goodnight")


async def test_run_by_name_clear_winner():
    rest = FakeRest()
    text = await cmd_run("living room lights on", _runner(rest))
    assert text == "Done — triggered Living Room Lights On."
    assert rest.calls[0][:2] == ("script", "ai_action_lights_on")


async def test_run_ambiguous_name_lists_candidates_with_actions_numbers():
    rest = FakeRest()
    text = await cmd_run("living room lights", _runner(rest))
    assert text.startswith("Did you mean:")
    assert "4. Living Room Lights On" in text
    assert "3. Living Room Lights Off" in text
    assert rest.calls == []


async def test_run_missing_required_params_explains():
    rest = FakeRest()
    text = await cmd_run("Dim", _runner(rest))
    assert text == "Dim needs: level. Example: /run Dim level=…"
    assert rest.calls == []


async def test_run_with_params():
    rest = FakeRest()
    text = await cmd_run("Dim level=30", _runner(rest))
    assert text == "Done — triggered Dim (level: 30)."
    assert rest.calls[0] == ("script", "ai_action_dim", None, {"level": 30})


async def test_run_refused_when_ai_switch_off():
    rest = FakeRest(switch="off")
    text = await cmd_run("2", _runner(rest))
    assert text.startswith("AI-triggered actions are turned off")
    assert rest.calls == []


async def test_run_unknown_number_and_no_match():
    assert await cmd_run("9", _runner(FakeRest())) == "No action number 9. See /actions."
    assert await cmd_run("espresso", _runner(FakeRest())) == "No action matches “espresso”. See /actions."


async def test_run_not_available_without_actions():
    text = await cmd_run("2", _runner(FakeRest(), allow_actions=False))
    assert text == "Not available in this home."


async def test_notes_and_cancel(store):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    note_id = store.add_state_note(
        entity_id="vacuum.robot", to_state="cleaning", instruction="Stop the vacuum.",
        instruction_original=None, kind="action", language="en",
        expires_at=now + timedelta(hours=24), now=now, source_thread_id="t1",
        action_entity_id="script.ai_action_stop_vacuum", reminder=None,
    )
    runner = _runner(FakeRest(), store)
    listed = await cmd_notes("", runner)
    assert f"#{note_id}" in listed and "Stop the vacuum." in listed
    assert await cmd_cancel(str(note_id), runner) == f"Cancelled task #{note_id}."
    assert await cmd_notes("", runner) == "No scheduled tasks."
    assert await cmd_cancel("abc", runner) == "Usage: /cancel <id>. See /notes."
