"""Availability probes for helper tools that depend on an entity existing in
this home (vacuum, battery, weather, person). Not a tool module — registers
nothing. Each probe is cheap (one REST list or one cached WS registry read)
and is called once at startup by registry.resolve_enabled.
"""

from __future__ import annotations

from app.tools.context import ws_ready


async def any_entity(ctx, prefix: str = "", device_class: str = "") -> bool:
    """True if any current state matches the given entity_id prefix and/or
    attributes.device_class. An empty filter matches everything."""
    states = await ctx.rest.list_states()
    for s in states:
        if prefix and not s["entity_id"].startswith(prefix):
            continue
        if device_class and s.get("attributes", {}).get("device_class") != device_class:
            continue
        return True
    return False


async def find_vacuum(ctx) -> str | None:
    """settings.vacuum_entity if set, otherwise the first vacuum.* entity
    (sorted by entity_id), or None if there is no vacuum in this home."""
    if ctx.settings.vacuum_entity:
        return ctx.settings.vacuum_entity
    states = await ctx.rest.list_states()
    vacuum_ids = []
    for s in states:
        if s["entity_id"].startswith("vacuum."):
            vacuum_ids.append(s["entity_id"])
    if not vacuum_ids:
        return None
    vacuum_ids.sort()
    return vacuum_ids[0]


async def room_sensor_for(ctx, vacuum_id: str) -> str | None:
    """The room sensor on the same device as vacuum_id, if one exists.

    Needs ws_ready. A sibling sensor.* qualifies when its entity_id ends with
    '_current_room' or its translation_key is 'current_room'.
    """
    if not ws_ready(ctx):
        return None
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    device_id = None
    for e in entities:
        if e["entity_id"] == vacuum_id:
            device_id = e.get("device_id")
            break
    if device_id is None:
        return None
    for e in entities:
        entity_id = e["entity_id"]
        if not entity_id.startswith("sensor."):
            continue
        if e.get("device_id") != device_id:
            continue
        if entity_id.endswith("_current_room") or e.get("translation_key") == "current_room":
            return entity_id
    return None
