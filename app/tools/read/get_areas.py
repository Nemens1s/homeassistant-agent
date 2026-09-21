"""The area/room-structure tool: room names, or the whole-home room→device map.
Consolidates what used to be several overlapping topology tools. The devices in
ONE specific room are list_devices' job — get_areas deliberately does not take a
room name, so a small model never has to choose between the two for that query."""

from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.lookups import device_name
from app.tools.registry import register


class Params(BaseModel):
    include_devices: bool = Field(
        default=False, description="Set true to return every room's devices (the whole-home map) instead of just room names."
    )


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error(
            "ws_unavailable",
            "Area lookup needs the websocket connection, which is not available.",
        )
    areas = await ctx.ws.request_cached("config/area_registry/list")

    # Mode 1: just the names — no device/entity walk needed.
    if not params.include_devices:
        names = []
        for a in areas:
            names.append(a["name"])
        return ToolResult.ok({"areas": names})

    all_devices = await ctx.ws.request_cached("config/device_registry/list")
    devices = []
    for d in all_devices:
        if not d.get("disabled_by"):
            devices.append(d)

    # Mode 2: full topology — every room with its device names. No entity IDs;
    # those are too numerous and overflow the context window for large installs.
    area_names = {}
    for a in areas:
        area_names[a["area_id"]] = a["name"]
    out: dict[str, list[str]] = {}
    for name in area_names.values():
        out[name] = []
    unassigned: list[str] = []
    for d in devices:
        bucket_name = area_names.get(d.get("area_id"))
        if bucket_name:
            out[bucket_name].append(device_name(d))
        else:
            unassigned.append(device_name(d))
    return ToolResult.ok({"areas": out, "unassigned": unassigned})


register(
    ToolDefinition(
        name="get_areas",
        description=(
            "Room/area STRUCTURE only — no HA entity_ids, no live states. "
            "No args → all room names. include_devices=true → the whole-home map "
            "(every room with its hardware device names). "
            "For the devices in ONE specific room, use list_devices(area=...) instead."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
