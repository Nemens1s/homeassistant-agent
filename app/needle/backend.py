"""Needle-specific helpers: name-map construction and result parsing.

Decision is defined in app.fast_path.backend (backend-agnostic); imported
here so callers that already import Decision from this module keep working."""

from __future__ import annotations

import re

from app.fast_path.backend import Decision  # noqa: F401 — re-exported for compat
from app.needle.menu import Menu

_NON_IDENT = re.compile(r"[^0-9a-zA-Z_]")


def _tool_name(entity_id: str) -> str:
    """Derive a python-identifier tool name from an automation entity_id.
    'automation.ai_goodnight' -> 'goodnight'."""
    local = entity_id
    if "." in local:
        local = local.split(".", 1)[1]
    if local.startswith("ai_"):
        local = local[len("ai_"):]
    local = _NON_IDENT.sub("_", local)
    if not local or local[0].isdigit():
        local = "a_" + local
    return local


def _build_name_map(menu: Menu) -> dict[str, str]:
    """Return tool_name -> entity_id with guaranteed-unique tool names."""
    mapping = {}
    used = set()
    for item in menu.items:
        base = _tool_name(item.entity_id)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        mapping[name] = item.entity_id
    return mapping


def _decision_from_result(result: dict, name_to_id: dict[str, str]) -> Decision:
    """Map a needle complete() result dict into a Decision."""
    confidence = float(result.get("confidence") or 0.0)
    calls = result.get("function_calls") or []
    if not calls:
        return Decision(entity_id=None, confidence=confidence)
    name = calls[0].get("name")
    entity_id = name_to_id.get(name)
    arguments = calls[0].get("arguments") or {}
    return Decision(entity_id=entity_id, confidence=confidence, arguments=arguments)


