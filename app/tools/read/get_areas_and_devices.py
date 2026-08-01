from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error(
            "ws_unavailable",
            "Area/device topology needs the websocket connection, which is not available.",
        )
    areas = await ctx.ws.request_cached("config/area_registry/list")
    devices = await ctx.ws.request_cached("config/device_registry/list")
    entities = await ctx.ws.request_cached("config/entity_registry/list")

    area_names = {a["area_id"]: a["name"] for a in areas}
    device_area = {d["id"]: d.get("area_id") for d in devices}
    device_name = {d["id"]: d.get("name_by_user") or d.get("name") or d["id"] for d in devices}

    out = {name: {"devices": [], "entities": []} for name in area_names.values()}
    unassigned = {"devices": [], "entities": []}

    for d in devices:
        bucket = out.get(area_names.get(d.get("area_id")))
        (bucket["devices"] if bucket else unassigned["devices"]).append(device_name[d["id"]])

    for e in entities:
        # entity's own area assignment overrides its device's area
        area_id = e.get("area_id") or device_area.get(e.get("device_id"))
        bucket = out.get(area_names.get(area_id))
        (bucket["entities"] if bucket else unassigned["entities"]).append(e["entity_id"])

    return ToolResult.ok({"areas": out, "unassigned": unassigned})


register(
    ToolDefinition(
        name="get_areas_and_devices",
        description="Get the full home topology: every area with its devices and entity_ids, plus unassigned items. Use to explore all rooms at once, discover which area a device belongs to, or get a complete home map. For entity states in one specific room, use list_entities with area= instead.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
