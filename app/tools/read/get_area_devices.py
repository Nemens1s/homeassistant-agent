from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    area_name: str


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error(
            "ws_unavailable",
            "Area/device lookup needs the websocket connection, which is not available.",
        )
    areas = await ctx.ws.request_cached("config/area_registry/list")
    devices = await ctx.ws.request_cached("config/device_registry/list")

    area = next(
        (a for a in areas if a["name"].lower() == params.area_name.lower()), None
    )
    if area is None:
        return ToolResult.error(
            "area_not_found",
            f"No area named {params.area_name!r}. Call get_areas_and_devices to list available areas.",
        )

    area_id = area["area_id"]
    device_name = {d["id"]: d.get("name_by_user") or d.get("name") or d["id"] for d in devices}

    result_devices = [
        device_name[d["id"]] for d in devices if d.get("area_id") == area_id
    ]

    return ToolResult.ok({"area": area["name"], "devices": result_devices})


register(
    ToolDefinition(
        name="get_area_devices",
        description="Get all devices and entity_ids in one specific area/room by name. Use for 'what devices are in the kitchen?' style questions.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
