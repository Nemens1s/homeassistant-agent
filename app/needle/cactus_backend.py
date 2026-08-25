"""In-process Needle backend (approach A) via the `cactus-needle` pip package.

Design notes from the feasibility spike (docs/superpowers/plans/needle-spike-notes.md):
- Needle matches on tool NAME + DESCRIPTION; an opaque entity_id enum yields
  near-zero confidence. So we model ONE described tool per ai_* automation and
  map the chosen tool name back to its entity_id.
- `complete()` proposes a call WITHOUT executing it (parse-only) — never `run()`.
- `complete()` blocks for ~2-4 s on a weak CPU, so it runs in a thread and must
  not stall the event loop.

The `needle` runtime is imported lazily (only when a real agent is built), so
this module and its pure helpers import fine without the dependency installed.
"""

from __future__ import annotations

import asyncio
import re

from app.needle.backend import Decision
from app.needle.menu import Menu

_NON_IDENT = re.compile(r"[^0-9a-zA-Z_]")


def _tool_name(entity_id: str) -> str:
    """Derive a python-identifier tool name from an automation entity_id.
    'automation.ai_goodnight' -> 'goodnight'. Needle uses the function __name__
    as the tool name the model selects."""
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
    """tool_name -> entity_id, with guaranteed-unique tool names (a collision
    would make the reverse mapping ambiguous)."""
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
    """Map a needle complete() result dict into a Decision. An unknown tool name
    (shouldn't happen — output is grammar-constrained) maps to entity_id None."""
    confidence = float(result.get("confidence") or 0.0)
    calls = result.get("function_calls") or []
    if not calls:
        return Decision(entity_id=None, confidence=confidence)
    name = calls[0].get("name")
    entity_id = name_to_id.get(name)
    return Decision(entity_id=entity_id, confidence=confidence)


def _make_tool(name: str, description: str):
    """Build a no-arg needle tool named `name` with `description` as its docstring
    (Needle matches on both). The body is never executed — we use complete()."""
    import needle

    def fn():
        return {"ok": True}

    fn.__name__ = name
    fn.__doc__ = description
    return needle.tool(fn)


def _default_agent_factory(model_path: str):
    """Return build(menu) -> (agent, name_to_id) using the real needle runtime."""

    def build(menu: Menu):
        import needle

        name_to_id = _build_name_map(menu)
        id_to_desc = {}
        for item in menu.items:
            id_to_desc[item.entity_id] = item.name
        tools = []
        for name, entity_id in name_to_id.items():
            tools.append(_make_tool(name, id_to_desc[entity_id]))
        if model_path:
            agent = needle.Needle(tools=tools, weights=model_path)
        else:
            agent = needle.Needle(tools=tools)
        return agent, name_to_id

    return build


class CactusBackend:
    """NeedleBackend backed by cactus-needle. Caches the built agent per
    menu.signature so the grammar/tools are rebuilt only when the ai_* set
    changes. `agent_factory` is injectable for tests (a fake agent needs only
    `reset()` and `complete(text) -> dict`)."""

    def __init__(self, model_path: str = "", agent_factory=None):
        self._agent_factory = agent_factory or _default_agent_factory(model_path)
        self._cache_signature = None
        self._agent = None
        self._name_to_id = {}

    def _agent_for(self, menu: Menu):
        if self._cache_signature != menu.signature:
            self._agent, self._name_to_id = self._agent_factory(menu)
            self._cache_signature = menu.signature
        return self._agent, self._name_to_id

    async def classify(self, message: str, menu: Menu) -> Decision:
        agent, name_to_id = self._agent_for(menu)
        result = await asyncio.to_thread(self._run_sync, agent, message)
        return _decision_from_result(result, name_to_id)

    def _run_sync(self, agent, message: str) -> dict:
        agent.reset()
        return agent.complete(message)
