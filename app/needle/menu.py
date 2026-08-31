"""The fast-path menu: the set of ai_* automations Needle may trigger, plus a
stable signature of that id set (used by a backend to cache its compiled
grammar). Fetched from HA REST and cached with a short TTL."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

from app.tools.action.trigger_automation import AI_AUTOMATION_PREFIX


@dataclass(frozen=True)
class MenuItem:
    entity_id: str
    name: str
    description: str = ""  # optional; Needle matches on name + description


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


def needle_description(ha_description: str) -> str:
    if "NEEDLE:" in ha_description:
        return ha_description.split("NEEDLE:")[-1].strip()
    return ha_description


async def _empty() -> str:
    return ""


async def _fetch_automation_description(rest, unique_id: str) -> str:
    try:
        config = await rest.get_automation_config(unique_id)
        return config.get("description") or ""
    except Exception:
        return ""


async def _entity_unique_ids(ws) -> dict[str, str]:
    """Return {entity_id: unique_id} from the entity registry, or {} on failure."""
    try:
        entries = await ws.request_cached("config/entity_registry/list")
        result = {}
        for entry in entries:
            uid = entry.get("unique_id") or ""
            if uid:
                result[entry["entity_id"]] = uid
        return result
    except Exception:
        return {}


class MenuProvider:
    def __init__(self, rest, ttl_s: int = 60, prefix: str = AI_AUTOMATION_PREFIX, ws=None):
        self._rest = rest
        self._ws = ws
        self._ttl_s = ttl_s
        self._prefix = prefix
        self._cached: Menu | None = None
        self._fetched_at = 0.0
        self._desc_cache: dict[str, str] = {}  # entity_id → description; session-persistent

    async def get(self) -> Menu:
        now = time.monotonic()
        if self._cached is not None and (now - self._fetched_at) < self._ttl_s:
            return self._cached
        states = await self._rest.list_states()
        candidates = []
        for state in states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith(self._prefix):
                continue
            attrs = state.get("attributes") or {}
            name = attrs.get("friendly_name") or entity_id
            candidates.append((entity_id, name))
        new_eids = [eid for eid, _ in candidates if eid not in self._desc_cache]
        if new_eids:
            unique_ids = await _entity_unique_ids(self._ws) if self._ws is not None else {}
            tasks = []
            for eid in new_eids:
                if eid in unique_ids:
                    tasks.append(_fetch_automation_description(self._rest, unique_ids[eid]))
                else:
                    tasks.append(_empty())
            fetched = await asyncio.gather(*tasks)
            for eid, desc in zip(new_eids, fetched):
                self._desc_cache[eid] = desc
        items = []
        for entity_id, name in candidates:
            raw_desc = self._desc_cache.get(entity_id, "")
            items.append(MenuItem(entity_id=entity_id, name=name, description=needle_description(raw_desc)))
        items.sort(key=_by_id)
        menu = Menu(items=tuple(items), signature=_signature(items))
        self._cached = menu
        self._fetched_at = now
        return menu
