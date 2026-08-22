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

    # Mode 3: full topology — every room with its device names. No entity IDs;
    # those are too numerous and overflow the context window for large installs.
    area_names = {a["area_id"]: a["name"] for a in areas}
    out: dict[str, list[str]] = {name: [] for name in area_names.values()}
    unassigned: list[str] = []
    for d in devices:
        bucket_name = area_names.get(d.get("area_id"))
        (out[bucket_name] if bucket_name else unassigned).append(_device_name(d))
    return ToolResult.ok({"areas": out, "unassigned": unassigned})


register(
    ToolDefinition(
        name="get_areas",
        description=(
            "Room/area names and hardware device names only — no HA entity_ids, no live states. "
            "No args → all room names. name='Kitchen' → devices in that room. "
            "include_devices=true → full home map. "
            "For HA entities of a specific device, use list_devices then list_entities(device=)."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
