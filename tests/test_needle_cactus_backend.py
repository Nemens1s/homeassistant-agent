import os

import pytest

from app.needle.cactus_backend import (
    CactusBackend,
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
    assert len(mapping) == 2                      # no clobbering
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


class FakeAgent:
    def __init__(self, result):
        self._result = result
        self.reset_calls = 0
        self.completed = []

    def reset(self):
        self.reset_calls += 1

    def complete(self, message):
        self.completed.append(message)
        return self._result


async def test_classify_uses_agent_and_maps_decision():
    menu = _menu("automation.ai_goodnight")
    agent = FakeAgent({"function_calls": [{"name": "goodnight"}], "confidence": 0.97})
    calls = {"n": 0}

    def factory(m):
        calls["n"] += 1
        return agent, {"goodnight": "automation.ai_goodnight"}

    backend = CactusBackend(agent_factory=factory)
    d = await backend.classify("goodnight", menu)
    assert d.entity_id == "automation.ai_goodnight" and d.confidence == 0.97
    assert agent.reset_calls == 1 and agent.completed == ["goodnight"]
    assert calls["n"] == 1


async def test_agent_is_cached_by_signature_and_rebuilt_on_change():
    agent = FakeAgent({"function_calls": [], "confidence": 1.0})
    calls = {"n": 0}

    def factory(m):
        calls["n"] += 1
        return agent, {}

    backend = CactusBackend(agent_factory=factory)
    menu_a = _menu("automation.ai_goodnight")
    await backend.classify("x", menu_a)
    await backend.classify("y", menu_a)      # same signature -> reuse
    assert calls["n"] == 1
    menu_b = _menu("automation.ai_goodnight", "automation.ai_movie")  # new signature
    await backend.classify("z", menu_b)
    assert calls["n"] == 2


@pytest.mark.skipif(not os.getenv("NEEDLE_LIVE"), reason="set NEEDLE_LIVE=1 to run the real runtime")
async def test_live_classify_returns_menu_id_or_none():
    pytest.importorskip("needle")
    menu = _menu("automation.ai_goodnight", "automation.ai_movie")
    backend = CactusBackend(model_path=os.getenv("NEEDLE_MODEL_PATH", ""))
    d = await backend.classify("goodnight", menu)
    assert d.entity_id in {None, "automation.ai_goodnight", "automation.ai_movie"}
    assert 0.0 <= d.confidence <= 1.0
