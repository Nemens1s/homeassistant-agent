from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(description="Full entity id, e.g. 'sensor.living_room_temperature'")
    start_time: str = Field(description="ISO8601 start, e.g. '2026-07-12T00:00:00'")
    end_time: str = Field(default="", description="ISO8601 end; empty means now")


async def handler(params: Params, ctx) -> ToolResult:
    data = await ctx.rest.get_history(
        params.entity_id, params.start_time, params.end_time or None
    )
    changes = data[0] if data else []
    rows = [{"state": c.get("state"), "at": c.get("last_changed", "")} for c in changes]
    return ToolResult.ok(
        bound_rows(rows, max_rows=ctx.settings.max_rows,
                   hint="Narrow the time range to see the rest.")
    )


register(
    ToolDefinition(
        name="get_history",
        description="Get historical state changes for one entity between two ISO8601 timestamps. Use the current time from the system prompt to compute ranges like 'yesterday'.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
