"""Tool catalogue. Tool modules call register() at import time; load_all()
imports them. Adding a tool = one new module + one line in _DEFAULT_MODULES.

tools_for_tier() is the permission gate: the agent only sees tools at or
below max_tier. At max_tier=1 that means READ-only; at max_tier=2 action
tools (control_entity, trigger_automation) are also included.
"""

from __future__ import annotations

import importlib
import sys

from app.tools.base import ToolDefinition

_REGISTRY: dict[str, ToolDefinition] = {}

_DEFAULT_MODULES: tuple[str, ...] = (
    "app.tools.read.get_entity_state",
    "app.tools.read.list_entities",
    "app.tools.read.get_history",
    "app.tools.read.get_logbook",
    "app.tools.read.get_error_log",
    "app.tools.read.get_areas",
    "app.tools.read.get_battery_status",
    "app.tools.read.get_automations",
    "app.tools.read.get_person_locations",
    "app.tools.read.get_vacuum_state",
    "app.tools.read.get_weather",
    "app.tools.read.search_entities",
    "app.tools.read.list_devices",
    "app.tools.read.list_skills",
    "app.tools.read.load_skill",
    "app.tools.action.control_entity",
    "app.tools.action.trigger_automation",
)


def register(tool: ToolDefinition) -> ToolDefinition:
    if tool.name in _REGISTRY:
        raise ValueError(f"tool already registered: {tool.name!r}")
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> ToolDefinition | None:
    return _REGISTRY.get(name)


def tools_for_tier(max_tier: int) -> list[ToolDefinition]:
    eligible = []
    for t in _REGISTRY.values():
        if t.tier <= max_tier:
            eligible.append(t)

    def sort_key(t):
        return t.name

    eligible.sort(key=sort_key)
    return eligible


def load_all(modules: tuple[str, ...] = _DEFAULT_MODULES) -> None:
    """Import every tool module so its register() call runs. Reload if already
    imported, so tests that reset the registry can re-trigger registration."""
    for mod in modules:
        existing = sys.modules.get(mod)
        if existing is not None:
            importlib.reload(existing)
        else:
            importlib.import_module(mod)


def _reset_for_tests() -> None:
    _REGISTRY.clear()
