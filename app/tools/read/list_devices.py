from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    area: str = Field(default="", description="Optional area/room name filter, e.g. 'Kitchen'.")
    name: str = Field(default="", description="Optional substring to search in device name, e.g. 'roborock'.")


def _device_name(d: dict) -> str:
    return d.get("name_by_user") or d.get("name") or d["id"]


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error("ws_unavailable", "Device listing needs the websocket connection.")

    devices = await ctx.ws.request_cached("config/device_registry/list")
    areas = await ctx.ws.request_cached("config/area_registry/list")
    area_names = {a["area_id"]: a["name"] for a in areas}

    if params.area:
        match = next((a for a in areas if a["name"].lower() == params.area.lower()), None)
        if match is None:
            return ToolResult.error(
                "area_not_found",
                f"No area named {params.area!r}.",
                data={"available_areas": [a["name"] for a in areas]},
            )
        devices = [d for d in devices if d.get("area_id") == match["area_id"]]

    if params.name:
        q = params.name.lower()
        devices = [d for d in devices if q in _device_name(d).lower()]

    rows = sorted(
        [{"name": _device_name(d), "area": area_names.get(d.get("area_id"), "")} for d in devices],
        key=lambda r: (r["area"], r["name"]),
    )
    return ToolResult.ok({"devices": rows, "total": len(rows)})


register(
    ToolDefinition(
        name="list_devices",
        description=(
            "List hardware devices (not HA entities). No args → all devices. area='Kitchen' → devices "
            "in that room. name='roborock' → search by device name. Returns device names — pass one to "
            "list_entities(device=...) to see its HA entities and states."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
