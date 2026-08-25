"""The fast-path menu: the set of ai_* automations Needle may trigger, plus a
stable signature of that id set (used by a backend to cache its compiled
grammar). Fetched from HA REST and cached with a short TTL."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from app.tools.action.trigger_automation import AI_AUTOMATION_PREFIX


@dataclass(frozen=True)
class MenuItem:
    entity_id: str
    name: str


@dataclass(frozen=True)
class Menu:
    items: tuple[MenuItem, ...]
    signature: str


def _signature(items: list[MenuItem]) -> str:
    parts = []
    for item in items:
        parts.append(item.entity_id)
    joined = "\n".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _by_id(item: MenuItem) -> str:
    return item.entity_id


class MenuProvider:
    def __init__(self, rest, ttl_s: int = 60, prefix: str = AI_AUTOMATION_PREFIX):
        self._rest = rest
        self._ttl_s = ttl_s
        self._prefix = prefix
        self._cached: Menu | None = None
        self._fetched_at = 0.0

    async def get(self) -> Menu:
        now = time.monotonic()
        if self._cached is not None and (now - self._fetched_at) < self._ttl_s:
            return self._cached
        states = await self._rest.list_states()
        items = []
        for state in states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith(self._prefix):
                continue
            attrs = state.get("attributes") or {}
            name = attrs.get("friendly_name") or entity_id
            items.append(MenuItem(entity_id=entity_id, name=name))
        items.sort(key=_by_id)
        menu = Menu(items=tuple(items), signature=_signature(items))
        self._cached = menu
        self._fetched_at = now
        return menu
