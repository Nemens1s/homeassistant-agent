"""Tool subsetting: the router must narrow the menu without ever hiding a tool the
model legitimately needs. The eval-corpus test below is the real guardrail."""

from pathlib import Path

import yaml

from app.agent.tool_router import CORE_TOOLS, select_tool_names, select_tools

ALL_TOOLS = {
    "search_entities", "get_entity_state", "list_entities", "load_skill",
    "trigger_automation", "get_battery_status", "get_vacuum_state",
    "get_weather", "get_person_locations", "get_history", "get_logbook",
    "get_error_log", "get_automations", "get_areas", "list_devices",
}

CASES = yaml.safe_load(
    (Path(__file__).parent / "evals" / "cases.yaml").read_text()
)


def test_core_tools_always_present():
    selected = select_tool_names(ALL_TOOLS, "some unrelated gibberish xyzzy")
    assert CORE_TOOLS <= selected


def test_battery_keyword_exposes_battery_tool():
    selected = select_tool_names(ALL_TOOLS, "What is Sofija's phone battery?")
    assert "get_battery_status" in selected


def test_unrelated_query_hides_specialized_tools():
    selected = select_tool_names(ALL_TOOLS, "Turn on the desk lamp")
    assert "get_vacuum_state" not in selected
    assert "get_weather" not in selected
    # ...but the general + action tools remain
    assert {"list_entities", "trigger_automation"} <= selected


def test_room_mention_exposes_area_tool():
    selected = select_tool_names(ALL_TOOLS, "What devices are in the bedroom?")
    assert "get_areas" in selected


def test_subsetting_is_a_strict_narrowing():
    selected = select_tool_names(ALL_TOOLS, "What happened in the house last hour?")
    assert selected <= ALL_TOOLS
    assert "get_logbook" in selected


def test_never_returns_empty():
    assert select_tool_names(ALL_TOOLS, "") == set(CORE_TOOLS) & ALL_TOOLS or select_tool_names(ALL_TOOLS, "")


def test_select_tools_preserves_objects_and_order():
    class T:
        def __init__(self, name):
            self.name = name

    tools = [T("list_entities"), T("get_weather"), T("get_battery_status")]
    kept = select_tools(tools, "what's the weather?")
    names = [t.name for t in kept]
    assert "get_weather" in names
    assert "list_entities" in names  # core
    assert names == [t.name for t in tools if t.name in names]  # order preserved


def test_router_never_hides_the_expected_eval_tool():
    """The safety guarantee: for every eval case, the tool the model is supposed to
    call survives subsetting. Subsetting must never turn a passing case into a fail."""
    failures = []
    for case in CASES:
        expected = case.get("expect_tool")
        if not expected:
            continue  # expect_not_tool cases have no positive target
        selected = select_tool_names(ALL_TOOLS, case["prompt"])
        if expected not in selected:
            failures.append(f"{case['id']}: {expected} hidden for {case['prompt']!r}")
    assert not failures, "router hid tools needed by eval cases:\n" + "\n".join(failures)
