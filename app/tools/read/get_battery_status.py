from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.people import alias_legend
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    battery_states = [
        s for s in states
        if s.get("attributes", {}).get("device_class") == "battery"
    ]

    if not battery_states:
        return ToolResult.ok({"batteries": []})

    # Build area lookup if WS is available
    area_by_entity: dict[str, str] = {}
    if ctx.ws is not None:
        areas = await ctx.ws.request_cached("config/area_registry/list")
        devices = await ctx.ws.request_cached("config/device_registry/list")
        entities = await ctx.ws.request_cached("config/entity_registry/list")
        area_name = {a["area_id"]: a["name"] for a in areas}
        device_area = {d["id"]: d.get("area_id") for d in devices}
        for e in entities:
            aid = e.get("area_id") or device_area.get(e.get("device_id"))
            if aid:
                area_by_entity[e["entity_id"]] = area_name.get(aid, "")

    results = []
    for s in battery_states:
        eid = s["entity_id"]
        attrs = s.get("attributes", {})
        results.append({
            "entity_id": eid,
            "name": attrs.get("friendly_name", eid),
            "state": s["state"],
            "unit": attrs.get("unit_of_measurement", ""),
            "area": area_by_entity.get(eid, ""),
            "last_changed": s.get("last_changed", ""),
        })

    results.sort(key=lambda r: (r["area"], r["name"]))
    data: dict = {"batteries": results}
    # Alias legend lets the agent map a query like "sonja's battery" to the
    # canonical person whose name appears in a device's friendly_name.
    aliases = alias_legend(ctx.settings)
    if aliases:
        data["name_aliases"] = aliases
    return ToolResult.ok(data)


register(
    ToolDefinition(
        name="get_battery_status",
        description="Get battery level for all battery-powered devices. Use for ANY battery question: 'what is Sofija's phone battery?', 'which batteries are low?', 'battery status'. Already returns every device (phones, remotes, sensors) — no need to look anything up first.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
