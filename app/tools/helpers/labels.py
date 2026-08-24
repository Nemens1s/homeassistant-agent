"""Label-based guardrail for action tools.

check_entity_labels() returns False only when allowed_labels is configured,
WS is available, AND the entity (or its device) carries none of the required
labels. Any other condition (WS down, allowed_labels empty) returns None so
callers fail-open.
"""

from __future__ import annotations


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
    label_name: dict[str, str] = {lb["label_id"]: lb["name"] for lb in label_list}

    entities = await ctx.ws.request_cached("config/entity_registry/list")
    entry = next((e for e in entities if e["entity_id"] == entity_id), None)

    entity_labels: set[str] = set()
    device_id: str | None = None
    if entry:
        entity_labels = {label_name.get(lid, lid) for lid in entry.get("labels", [])}
        device_id = entry.get("device_id")

    device_labels: set[str] = set()
    if device_id:
        devices = await ctx.ws.request_cached("config/device_registry/list")
        device = next((d for d in devices if d["id"] == device_id), None)
        if device:
            device_labels = {label_name.get(lid, lid) for lid in device.get("labels", [])}

    all_labels = entity_labels | device_labels
    return bool(all_labels & set(allowed_labels))


async def entity_label_names(entity_id: str, ctx) -> list[str]:
    """Return the resolved label names for entity_id (entity + device labels combined).
    Returns [] when WS is unavailable."""
    if ctx.ws is None:
        return []

    label_list = await ctx.ws.request_cached("config/label_registry/list")
    label_name: dict[str, str] = {lb["label_id"]: lb["name"] for lb in label_list}

    entities = await ctx.ws.request_cached("config/entity_registry/list")
    entry = next((e for e in entities if e["entity_id"] == entity_id), None)

    entity_labels: set[str] = set()
    device_id: str | None = None
    if entry:
        entity_labels = {label_name.get(lid, lid) for lid in entry.get("labels", [])}
        device_id = entry.get("device_id")

    device_labels: set[str] = set()
    if device_id:
        devices = await ctx.ws.request_cached("config/device_registry/list")
        device = next((d for d in devices if d["id"] == device_id), None)
        if device:
            device_labels = {label_name.get(lid, lid) for lid in device.get("labels", [])}

    return sorted(entity_labels | device_labels)
