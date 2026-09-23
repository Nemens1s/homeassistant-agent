"""The fast-path menu: the set of ai_* automations/scripts Needle may trigger, plus a
stable signature of that id set (used by a backend to cache its compiled
grammar). Fetched from HA REST and cached with a short TTL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

from app.constants import AI_AUTOMATION_PREFIX


@dataclass(frozen=True)
class MenuItem:
    entity_id: str
    name: str
    description: str = ""  # optional; Needle matches on name + description
    parameters: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Menu:
    items: tuple[MenuItem, ...]
    signature: str


def _signature(items: list[MenuItem]) -> str:
    parts = []
    for item in items:
        parts.append(item.entity_id + "\x1f" + json.dumps(item.parameters, sort_keys=True))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _by_id(item: MenuItem) -> str:
    return item.entity_id


def needle_description(ha_description: str) -> str:
    if "NEEDLE:" in ha_description:
        return ha_description.split("NEEDLE:")[-1].strip()
    return ha_description


def fields_to_parameters(fields: dict) -> dict:
    """HA script `fields:` -> JSON-Schema `parameters` object."""
    properties: dict = {}
    required: list[str] = []
    for name, spec in (fields or {}).items():
        spec = spec or {}
        selector = spec.get("selector") or {}
        schema: dict
        if "select" in selector:
            raw_options = (selector["select"] or {}).get("options", [])
            enum_values = [o["value"] if isinstance(o, dict) else o for o in raw_options]
            schema = {"type": "string", "enum": enum_values}
        elif "number" in selector:
            num = selector["number"] or {}
            schema = {"type": "integer"}
            if "min" in num:
                schema["minimum"] = num["min"]
            if "max" in num:
                schema["maximum"] = num["max"]
        else:
            schema = {"type": "string"}
        if spec.get("description"):
            schema["description"] = spec["description"]
        properties[name] = schema
        if spec.get("required"):
            required.append(name)
    out: dict = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


async def _empty_meta() -> tuple[str, dict]:
    return "", {}


async def _fetch_automation_meta(rest, unique_id: str) -> tuple[str, dict]:
    try:
        config = await rest.get_automation_config(unique_id)
        return config.get("description") or "", {}
    except Exception:
        return "", {}


async def _fetch_script_meta(rest, object_id: str) -> tuple[str, dict]:
    try:
        config = await rest.get_script_config(object_id)
        return config.get("description") or "", fields_to_parameters(config.get("fields") or {})
    except Exception:
        return "", {}


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
    def __init__(self, rest, ttl_s: int = 60, prefixes=(AI_AUTOMATION_PREFIX,), ws=None):
        self._rest = rest
        self._ws = ws
        self._ttl_s = ttl_s
        self._prefixes = tuple(prefixes)
        self._cached: Menu | None = None
        self._fetched_at = 0.0
        self._desc_cache: dict[str, str] = {}  # entity_id → description; session-persistent
        self._params_cache: dict[str, dict] = {}  # entity_id → parameters; session-persistent

    async def get(self) -> Menu:
        now = time.monotonic()
        if self._cached is not None and (now - self._fetched_at) < self._ttl_s:
            return self._cached
        states = await self._rest.list_states()
        candidates = []
        for state in states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith(self._prefixes):
                continue
            attrs = state.get("attributes") or {}
            name = attrs.get("friendly_name") or entity_id
            candidates.append((entity_id, name))
        new_eids = [eid for eid, _ in candidates if eid not in self._desc_cache]
        if new_eids:
            unique_ids = await _entity_unique_ids(self._ws) if self._ws is not None else {}
            tasks = []
            for eid in new_eids:
                if eid.startswith("script."):
                    tasks.append(_fetch_script_meta(self._rest, eid.split(".", 1)[1]))
                elif eid in unique_ids:
                    tasks.append(_fetch_automation_meta(self._rest, unique_ids[eid]))
                else:
                    tasks.append(_empty_meta())
            fetched = await asyncio.gather(*tasks)
            for eid, (desc, params) in zip(new_eids, fetched):
                self._desc_cache[eid] = desc
                self._params_cache[eid] = params
        items = []
        for entity_id, name in candidates:
            items.append(MenuItem(
                entity_id=entity_id,
                name=name,
                description=needle_description(self._desc_cache.get(entity_id, "")),
                parameters=self._params_cache.get(entity_id, {}),
            ))
        items.sort(key=_by_id)
        menu = Menu(items=tuple(items), signature=_signature(items))
        self._cached = menu
        self._fetched_at = now
        return menu
