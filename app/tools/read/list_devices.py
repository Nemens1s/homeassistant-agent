from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.lookups import device_name
from app.tools.registry import register


class Params(BaseModel):
    area: str = Field(default="", description="Optional area/room name filter, e.g. 'Kitchen'.")
    name: str = Field(default="", description="Optional substring to search in device name, e.g. 'roborock'.")


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error("ws_unavailable", "Device listing needs the websocket connection.")

    devices = await ctx.ws.request_cached("config/device_registry/list")
    areas = await ctx.ws.request_cached("config/area_registry/list")
    area_names = {}
    for a in areas:
        area_names[a["area_id"]] = a["name"]

    if params.area:
        match = None
        for a in areas:
            if a["name"].lower() == params.area.lower():
                match = a
                break
        if match is None:
            available = []
            for a in areas:
                available.append(a["name"])
            return ToolResult.error(
                "area_not_found",
                f"No area named {params.area!r}.",
                data={"available_areas": available},
            )
        filtered = []
        for d in devices:
            if d.get("area_id") == match["area_id"]:
                filtered.append(d)
        devices = filtered

    if params.name:
        q = params.name.lower()
        filtered = []
        for d in devices:
            if q in device_name(d).lower():
                filtered.append(d)
        devices = filtered

    rows = []
    for d in devices:
        rows.append({"name": device_name(d), "area": area_names.get(d.get("area_id"), "")})

    def sort_key(r):
        return (r["area"], r["name"])

    rows.sort(key=sort_key)
    return ToolResult.ok({"devices": rows, "total": len(rows)})


register(
    ToolDefinition(
        name="list_devices",
        description=(
            "List hardware devices (not HA entities), device names only. THE tool for "
            "'what's in <room>' / 'what do I have' — a clean device-level view without noisy entities. "
            "No args → all devices. area='Kitchen' → devices in that room. name='roborock' → search by name. "
            "Drilling into one device's individual entities+states is a separate, narrower lookup."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
