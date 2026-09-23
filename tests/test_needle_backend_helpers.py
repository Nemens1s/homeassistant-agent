from app.needle.backend import (
    _build_name_map,
    _decision_from_result,
    _tool_name,
)
from app.needle.menu import Menu, MenuItem


def _menu(*ids):
    items = []
    for entity_id in ids:
        items.append(MenuItem(entity_id, entity_id.split("ai_")[-1].title()))
    return Menu(items=tuple(items), signature="|".join(ids))


def test_tool_name_strips_prefix_and_sanitises():
    assert _tool_name("automation.ai_goodnight") == "goodnight"
    assert _tool_name("automation.ai_movie_time") == "movie_time"
    assert _tool_name("automation.ai_2nd floor") == "a_2nd_floor"  # digit-first + space


def test_build_name_map_is_unique_and_reversible():
    # Two ids that sanitise to the same base must get distinct tool names.
    menu = _menu("automation.ai_movie-time", "automation.ai_movie.time")
    mapping = _build_name_map(menu)
    assert len(mapping) == 2
    assert set(mapping.values()) == {"automation.ai_movie-time", "automation.ai_movie.time"}


def test_decision_from_result_maps_call_to_entity_id():
    name_to_id = {"goodnight": "automation.ai_goodnight"}
    result = {"function_calls": [{"name": "goodnight"}], "confidence": 0.9}
    d = _decision_from_result(result, name_to_id)
    assert d.entity_id == "automation.ai_goodnight" and d.confidence == 0.9


def test_decision_from_result_no_call_is_none():
    d = _decision_from_result({"function_calls": [], "confidence": 0.99}, {})
    assert d.entity_id is None and d.confidence == 0.99


def test_decision_from_result_unknown_name_is_none():
    d = _decision_from_result({"function_calls": [{"name": "ghost"}], "confidence": 0.5}, {})
    assert d.entity_id is None and d.confidence == 0.5


def test_decision_carries_arguments():
    name_to_id = {"lights_on": "script.ai_action_lights_on"}
    result = {"confidence": 0.9,
              "function_calls": [{"name": "lights_on", "arguments": {"room": "living_room"}}]}
    decision = _decision_from_result(result, name_to_id)
    assert decision.entity_id == "script.ai_action_lights_on"
    assert decision.arguments == {"room": "living_room"}


def test_decision_arguments_default_empty():
    decision = _decision_from_result({"confidence": 0.0, "function_calls": []}, {})
    assert decision.arguments == {}
