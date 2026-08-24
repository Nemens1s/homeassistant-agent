"""The single area/room tool. Consolidates what used to be three overlapping tools
(get_areas, get_area_devices, get_areas_and_devices) — a small model could not
reliably route among them. One tool, three modes selected by the arguments."""

from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.lookups import device_name
from app.tools.registry import register


class Params(BaseModel):
    name: str = Field(
        default="", description="A specific room/area name to inspect, e.g. 'Kitchen'. Empty = all areas."
    )
    include_devices: bool = Field(
        default=False, description="When no name is given, set true to return every room's devices instead of just names."
    )


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error(
            "ws_unavailable",
            "Area lookup needs the websocket connection, which is not available.",
        )
    areas = await ctx.ws.request_cached("config/area_registry/list")

    # Mode 1: just the names — no device/entity walk needed.
    if not params.name and not params.include_devices:
        names = []
        for a in areas:
            names.append(a["name"])
        return ToolResult.ok({"areas": names})

    all_devices = await ctx.ws.request_cached("config/device_registry/list")
    devices = []
    for d in all_devices:
        if not d.get("disabled_by"):
            devices.append(d)

    # Mode 2: one specific room → its DEVICES (hardware) only. Room questions stay
    # device-level here (and in list_devices); entity-level detail is only for
    # drilling into one device via list_entities(device=).
    if params.name:
        area = None
        for a in areas:
            if a["name"].lower() == params.name.lower():
                area = a
                break
        if area is None:
            available = []
            for a in areas:
                available.append(a["name"])
            return ToolResult.error(
                "area_not_found",
                f"No area named {params.name!r}.",
                data={"available_areas": available},
            )
        area_id = area["area_id"]
        room_devices = []
        for d in devices:
            if d.get("area_id") == area_id:
                room_devices.append(device_name(d))
        return ToolResult.ok({
            "area": area["name"],
            "devices": room_devices,
        })

    # Mode 3: full topology — every room with its device names. No entity IDs;
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
            "Room/area names and hardware device names only — no HA entity_ids, no live states. "
            "No args → all room names. name='Kitchen' → devices in that room. "
            "include_devices=true → full home map. "
            "Stop here for a room overview; individual entity_ids and states are a separate, narrower lookup."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
