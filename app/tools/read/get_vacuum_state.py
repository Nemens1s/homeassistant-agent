import httpx
from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.probes import find_vacuum, room_sensor_for
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    vacuum_id = await find_vacuum(ctx)
    if vacuum_id is None:
        return ToolResult.error("no_vacuum", "No vacuum entity found in Home Assistant.")

    vacuum = await ctx.rest.get_state(vacuum_id)
    data: dict = {
        "entity_id": vacuum["entity_id"],
        "state": vacuum["state"],
        "friendly_name": vacuum.get("attributes", {}).get("friendly_name", ""),
    }

    room_sensor_id = await room_sensor_for(ctx, vacuum_id)
    if room_sensor_id is not None:
        try:
            room = await ctx.rest.get_state(room_sensor_id)
            room_val = room.get("state", "")
            if room_val and room_val not in ("unknown", "unavailable"):
                data["current_room"] = room_val
        except httpx.HTTPStatusError:
            pass
    return ToolResult.ok(data)


async def _has_vacuum(ctx) -> bool:
    return await find_vacuum(ctx) is not None


register(
    ToolDefinition(
        name="get_vacuum_state",
        description=(
            "Get the robot vacuum's CURRENT state and room. "
            "Use only for present-tense questions: is it cleaning right now, where is it, has it just docked. "
            "For historical questions ('did it run yesterday?', 'when did it last clean?'), use get_history instead."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
        available=_has_vacuum,
    )
)
