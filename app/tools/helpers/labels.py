"""Label-based guardrail for action tools.

check_entity_labels() returns False only when allowed_labels is configured,
WS is available, AND the entity (or its device) carries none of the required
labels. Any other condition (WS down, allowed_labels empty) returns None so
callers fail-open.
"""

from __future__ import annotations


def _build_label_name(label_list: list) -> dict:
    label_name = {}
    for lb in label_list:
        label_name[lb["label_id"]] = lb["name"]
    return label_name


def _resolve_names(label_ids: list, label_name: dict) -> set:
    names = set()
    for lid in label_ids:
        names.add(label_name.get(lid, lid))
    return names


def _find_entity(entities: list, entity_id: str) -> dict | None:
    for e in entities:
        if e["entity_id"] == entity_id:
            return e
    return None


def _find_device(devices: list, device_id: str) -> dict | None:
    for d in devices:
        if d["id"] == device_id:
            return d
    return None


async def check_entity_labels(entity_id: str, allowed_labels: list[str], ctx) -> bool | None:
    """Check whether entity_id (or its device) has at least one allowed label.

    Returns:
        None  — check skipped (allowed_labels empty or WS unavailable); caller should allow.
        True  — entity/device carries at least one required label.
        False — entity/device carries none of the required labels; caller should deny.
    """
    if not allowed_labels or ctx.ws is None:
        return None

    label_list = await ctx.ws.request_cached("config/label_registry/list")
    label_name = _build_label_name(label_list)

    entities = await ctx.ws.request_cached("config/entity_registry/list")
    entry = _find_entity(entities, entity_id)

    entity_labels: set[str] = set()
    device_id: str | None = None
    if entry:
        entity_labels = _resolve_names(entry.get("labels", []), label_name)
        device_id = entry.get("device_id")

    device_labels: set[str] = set()
    if device_id:
        devices = await ctx.ws.request_cached("config/device_registry/list")
        device = _find_device(devices, device_id)
        if device:
            device_labels = _resolve_names(device.get("labels", []), label_name)

    all_labels = entity_labels | device_labels
    return bool(all_labels & set(allowed_labels))


async def entity_label_names(entity_id: str, ctx) -> list[str]:
    """Return the resolved label names for entity_id (entity + device labels combined).
    Returns [] when WS is unavailable."""
    if ctx.ws is None:
        return []

    label_list = await ctx.ws.request_cached("config/label_registry/list")
    label_name = _build_label_name(label_list)

    entities = await ctx.ws.request_cached("config/entity_registry/list")
    entry = _find_entity(entities, entity_id)

    entity_labels: set[str] = set()
    device_id: str | None = None
    if entry:
        entity_labels = _resolve_names(entry.get("labels", []), label_name)
        device_id = entry.get("device_id")

    device_labels: set[str] = set()
    if device_id:
        devices = await ctx.ws.request_cached("config/device_registry/list")
        device = _find_device(devices, device_id)
        if device:
            device_labels = _resolve_names(device.get("labels", []), label_name)

    return sorted(entity_labels | device_labels)
