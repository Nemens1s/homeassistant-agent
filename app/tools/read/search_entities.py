from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows, entity_domain
from app.tools.registry import register


class Params(BaseModel):
    query: str = Field(description="Search term matched case-insensitively against entity_id and friendly name.")


async def _registry_maps(ctx) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return ({entity_id: area_name}, {entity_id: [label_names]}) from cached WS registries."""
    areas = await ctx.ws.request_cached("config/area_registry/list")
    devices = await ctx.ws.request_cached("config/device_registry/list")
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    labels = await ctx.ws.request_cached("config/label_registry/list")

    area_name = {a["area_id"]: a["name"] for a in areas}
    label_name = {lb["label_id"]: lb["name"] for lb in labels}
    device_area = {d["id"]: d.get("area_id") for d in devices}
    device_labels: dict[str, list[str]] = {
        d["id"]: [label_name.get(lid, lid) for lid in d.get("labels", [])]
        for d in devices
    }

    area_map: dict[str, str] = {}
    label_map: dict[str, list[str]] = {}
    for e in entities:
        eid = e["entity_id"]
        area_id = e.get("area_id") or device_area.get(e.get("device_id"))
        if area_id and area_id in area_name:
            area_map[eid] = area_name[area_id]
        entity_lbls = [label_name.get(lid, lid) for lid in e.get("labels", [])]
        dev_lbls = device_labels.get(e.get("device_id", ""), [])
        all_lbls = sorted(set(entity_lbls) | set(dev_lbls))
        if all_lbls:
            label_map[eid] = all_lbls

    return area_map, label_map


async def handler(params: Params, ctx) -> ToolResult:
    words = params.query.lower().split()
    states = await ctx.rest.list_states()
    matches = [
        s for s in states
        if any(
            w in s["entity_id"].lower()
            or w in s.get("attributes", {}).get("friendly_name", "").lower()
            for w in words
        )
    ]
    area_map: dict[str, str] = {}
    label_map: dict[str, list[str]] = {}
    if ctx.ws is not None:
        area_map, label_map = await _registry_maps(ctx)
    controllable = {d for d in ctx.settings.allowed_domains if d != "automation"}
    rows = []
    for s in matches:
        domain = entity_domain(s["entity_id"])
        row: dict = {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
            "controllable": domain in controllable,
        }
        if ctx.ws is not None:
            row["area"] = area_map.get(s["entity_id"], "")
            lbls = label_map.get(s["entity_id"])
            if lbls:
                row["labels"] = lbls
        rows.append(row)
    return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))


register(
    ToolDefinition(
        name="search_entities",
        description=(
            "Search entities by name or entity_id keyword when the exact entity_id is unknown. Returns state and area for each match. "
            "Use when the user names a specific device but you don't know its entity_id (e.g. 'zigbee bridge', 'sleeping helper', 'tapo'). "
            "Not for automation queries (use get_automations) or battery questions (use get_battery_status)."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
