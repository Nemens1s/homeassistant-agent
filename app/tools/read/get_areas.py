"""The single area/room tool. Consolidates what used to be three overlapping tools
(get_areas, get_area_devices, get_areas_and_devices) — a small model could not
reliably route among them. One tool, three modes selected by the arguments."""

from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    name: str = Field(
        default="", description="A specific room/area name to inspect, e.g. 'Kitchen'. Empty = all areas."
    )
    include_devices: bool = Field(
        default=False, description="When no name is given, set true to return every room's devices and entities (full topology) instead of just names."
    )


def _device_name(d: dict) -> str:
    return d.get("name_by_user") or d.get("name") or d["id"]


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error(
            "ws_unavailable",
            "Area lookup needs the websocket connection, which is not available.",
        )
    areas = await ctx.ws.request_cached("config/area_registry/list")

    # Mode 1: just the names — no device/entity walk needed.
    if not params.name and not params.include_devices:
        return ToolResult.ok({"areas": [a["name"] for a in areas]})

    devices = await ctx.ws.request_cached("config/device_registry/list")

    # Mode 2: one specific room → its DEVICES (hardware) only. Entities-with-state
    # for a room are list_entities(area=)'s job — keeping this devices-only is what
    # makes the two tools genuinely non-overlapping.
    if params.name:
        area = next((a for a in areas if a["name"].lower() == params.name.lower()), None)
        if area is None:
            return ToolResult.error(
                "area_not_found",
                f"No area named {params.name!r}.",
                data={"available_areas": [a["name"] for a in areas]},
            )
        area_id = area["area_id"]
        return ToolResult.ok({
            "area": area["name"],
            "devices": [_device_name(d) for d in devices if d.get("area_id") == area_id],
        })

    # Mode 3: full topology — every room with its devices and entities, plus unassigned.
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    area_names = {a["area_id"]: a["name"] for a in areas}
    device_area = {d["id"]: d.get("area_id") for d in devices}

    # entity's own area assignment overrides its device's area
    def entity_area(e: dict) -> str | None:
        return e.get("area_id") or device_area.get(e.get("device_id"))

    out = {name: {"devices": [], "entities": []} for name in area_names.values()}
    unassigned = {"devices": [], "entities": []}
    for d in devices:
        bucket = out.get(area_names.get(d.get("area_id"))) or unassigned
        bucket["devices"].append(_device_name(d))
    for e in entities:
        bucket = out.get(area_names.get(entity_area(e))) or unassigned
        bucket["entities"].append(e["entity_id"])
    return ToolResult.ok({"areas": out, "unassigned": unassigned})


register(
    ToolDefinition(
        name="get_areas",
        description=(
            "Explore the physical layout of rooms/areas. No arguments → list all area "
            "names. name='Kitchen' → the DEVICES (hardware) in that one room. "
            "include_devices=true (no name) → the full home topology (every room's "
            "devices and entity_ids). To list the ENTITIES in a room or check their "
            "on/off states, use list_entities with area= — NOT this tool."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
