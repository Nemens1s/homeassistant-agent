"""Lookups over HA's config registries (area / device / entity).

These read the live registries via ctx.ws.request_cached — nothing here is
hardcoded, so a change in HA (renamed room, moved device) is reflected on the
next cached fetch. Kept in one place so tools that resolve rooms/devices to
entities (list_entities, search_entities, ...) don't each re-derive the logic.
Not a tool module — registers nothing.
"""

from __future__ import annotations


def device_name(d: dict) -> str:
    """The name HA shows for a device: user override, else integration name, else id."""
    return d.get("name_by_user") or d.get("name") or d["id"]


async def resolve_area(ctx, name: str) -> tuple[dict | None, list[str]]:
    """Look up an area by name (case-insensitive exact match).

    Returns (area_dict | None, all_area_names). The name list feeds a
    did-you-mean hint when the lookup misses.
    """
    areas = await ctx.ws.request_cached("config/area_registry/list")
    match = next((a for a in areas if a["name"].lower() == name.lower()), None)
    return match, [a["name"] for a in areas]


async def area_names(ctx) -> dict[str, str]:
    """{area_id: area_name} for every area."""
    areas = await ctx.ws.request_cached("config/area_registry/list")
    return {a["area_id"]: a["name"] for a in areas}


async def entity_area_ids(
    ctx, *, exclude_categories: tuple[str, ...] = ("diagnostic", "config")
) -> dict[str, str]:
    """{entity_id: area_id} honoring the entity's own area override, then its
    device's area. Entities with no resolvable area are omitted.

    exclude_categories drops entity categories from the result (diagnostic/config
    by default, matching the device filter); pass () to include everything.
    """
    devices = await ctx.ws.request_cached("config/device_registry/list")
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    device_area = {d["id"]: d.get("area_id") for d in devices}
    out: dict[str, str] = {}
    for e in entities:
        if e.get("entity_category") in exclude_categories:
            continue
        area_id = e.get("area_id") or device_area.get(e.get("device_id"))
        if area_id:
            out[e["entity_id"]] = area_id
    return out
