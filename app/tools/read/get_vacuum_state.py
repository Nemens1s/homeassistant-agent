import httpx
from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register

_VACUUM_ENTITY = "vacuum.roborock_qrevo_s"
_ROOM_SENSOR = "sensor.roborock_qrevo_s_current_room"


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    vacuum = await ctx.rest.get_state(_VACUUM_ENTITY)
    data: dict = {
        "entity_id": vacuum["entity_id"],
        "state": vacuum["state"],
        "friendly_name": vacuum.get("attributes", {}).get("friendly_name", ""),
    }
    try:
        room = await ctx.rest.get_state(_ROOM_SENSOR)
        room_val = room.get("state", "")
        if room_val and room_val not in ("unknown", "unavailable"):
            data["current_room"] = room_val
    except httpx.HTTPStatusError:
        pass
    return ToolResult.ok(data)


register(
    ToolDefinition(
        name="get_vacuum_state",
        description=(
            "Get the Roborock vacuum cleaner state and current room. "
            "Use for any question about the vacuum: where it is, what it is doing, whether it has finished cleaning."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
