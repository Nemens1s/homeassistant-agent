from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register
from app.tools.timerange import TimeRange, resolve_range


class Params(BaseModel):
    entity_id: str = Field(description="Full entity id, e.g. 'sensor.living_room_temperature'")
    range: TimeRange = Field(
        default="last_24h",
        description="How far back to look: last_hour, last_24h, today, yesterday, last_7d, or last_30d.",
    )


async def handler(params: Params, ctx) -> ToolResult:
    start_time, end_time = resolve_range(params.range)
    data = await ctx.rest.get_history(params.entity_id, start_time, end_time)
    changes = data[0] if data else []
    rows = [{"state": c.get("state"), "at": c.get("last_changed", "")} for c in changes]
    return ToolResult.ok(
        bound_rows(rows, max_rows=ctx.settings.max_rows,
                   hint="Narrow the time range to see the rest.")
    )


register(
    ToolDefinition(
        name="get_history",
        description="Get state change history for ONE specific entity over a relative time window (range=last_hour/last_24h/today/yesterday/last_7d/last_30d). Use only when an entity_id is known and the user asks for a timeseries (e.g. 'temperature history'). NOT for 'what happened in the house?' — use get_logbook for that.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
