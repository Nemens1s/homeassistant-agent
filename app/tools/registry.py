"""Tool catalogue. Tool modules call register() at import time; load_all()
imports them. Adding a tool = one new module + one line in _DEFAULT_MODULES.

tools_for_tier() is the permission gate: the agent only sees tools at or
below max_tier. At max_tier=1 that means READ-only; at max_tier=2 the action
tool (trigger_action) is also included.
"""

from __future__ import annotations

import importlib
import logging
import sys

from app.tools.base import ToolDefinition

log = logging.getLogger("agent.tools")

_REGISTRY: dict[str, ToolDefinition] = {}

_DEFAULT_MODULES: tuple[str, ...] = (
    "app.tools.read.get_entity_state",
    "app.tools.read.list_entities",
    "app.tools.read.get_history",
    "app.tools.read.get_activity",
    "app.tools.read.get_error_log",
    "app.tools.read.get_areas",
    "app.tools.read.get_battery_status",
    "app.tools.read.get_automations",
    "app.tools.read.list_actions",
    "app.tools.read.get_person_locations",
    "app.tools.read.get_vacuum_state",
    "app.tools.read.get_weather",
    "app.tools.read.search_entities",
    "app.tools.read.list_devices",
    "app.tools.read.load_skill",
    "app.tools.memory.schedule_on_state_change",
    "app.tools.memory.schedule_at_time",
    "app.tools.memory.list_scheduled",
    "app.tools.memory.cancel_scheduled",
    "app.tools.action.trigger_action",
    "app.tools.action.notify_user",
)


def register(tool: ToolDefinition) -> ToolDefinition:
    if tool.name in _REGISTRY:
        raise ValueError(f"tool already registered: {tool.name!r}")
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> ToolDefinition | None:
    return _REGISTRY.get(name)


def all_tools() -> list[ToolDefinition]:
    result = []
    for t in _REGISTRY.values():
        result.append(t)
    return result


def tools_for_tier(max_tier: int) -> list[ToolDefinition]:
    eligible = []
    for t in _REGISTRY.values():
        if t.tier <= max_tier:
            eligible.append(t)

    def sort_key(t):
        return t.name

    eligible.sort(key=sort_key)
    return eligible


async def resolve_enabled(ctx, quiet: bool = False) -> set[str]:
    """Tool names to offer in this home: allowed by tier, not switched off by
    the user, and (for helper tools) backed by a matching entity. A probe that
    fails keeps its tool — better a useless tool than a missing one.
    `quiet` skips the per-tool "not registered" line, for the periodic
    refresh, which logs only what changed."""
    disabled = set(ctx.settings.disabled_tools)
    enabled: set[str] = set()
    for t in tools_for_tier(ctx.settings.max_tier):
        if t.name in disabled:
            continue
        if t.available is None:
            enabled.add(t.name)
            continue
        try:
            ok = await t.available(ctx)
        except Exception as exc:
            log.warning("tool=%s availability probe failed (%s); keeping it", t.name, exc)
            ok = True
        if ok:
            enabled.add(t.name)
        elif not quiet:
            log.info("tool=%s not registered: nothing in this home to use it on", t.name)
    return enabled


async def refresh_enabled(ctx, enabled: set[str]) -> bool:
    """Re-run the availability probes and update `enabled` IN PLACE (the
    router and event runner hold the same set). Returns True when it changed,
    i.e. when the agent must be rebuilt to offer the new tool list. A home
    that was half-booted at startup gets its missing tools back this way."""
    fresh = await resolve_enabled(ctx, quiet=True)
    if fresh == enabled:
        return False
    added = sorted(fresh - enabled)
    removed = sorted(enabled - fresh)
    log.info("tools changed: added=%s removed=%s", added, removed)
    enabled.clear()
    enabled.update(fresh)
    return True


def load_all(modules: tuple[str, ...] = _DEFAULT_MODULES) -> None:
    """Import every tool module so its register() call runs. Clears first so
    it is safe to call even when side-effect imports have already registered
    some tools (e.g. trigger_action via needle/menu.py)."""
    _REGISTRY.clear()
    for mod in modules:
        existing = sys.modules.get(mod)
        if existing is not None:
            importlib.reload(existing)
        else:
            importlib.import_module(mod)


def _reset_for_tests() -> None:
    _REGISTRY.clear()
