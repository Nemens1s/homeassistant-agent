from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register
from app.tools.timerange import TimeRange, resolve_range


class Params(BaseModel):
    range: TimeRange = Field(
        default="last_24h",
        description="How far back to look: last_hour, last_24h, today, yesterday, last_7d, or last_30d.",
    )
    entity_id: str | None = Field(
        default=None,
        description="Filter to a single entity, e.g. 'fan.air_purifier'. Omit to get house-wide activity.",
    )


async def handler(params: Params, ctx) -> ToolResult:
    start_time, end_time = resolve_range(params.range)
    entries = await ctx.rest.get_logbook(start_time, end_time, params.entity_id)
    rows = [
        {
            "at": e.get("when", ""),
            "name": e.get("name", ""),
            "message": e.get("message", ""),
            "entity_id": e.get("entity_id", ""),
        }
        for e in entries
    ]
    return ToolResult.ok(
        bound_rows(rows, max_rows=ctx.settings.max_rows,
                   hint="Narrow the time range to see the rest.")
    )


register(
    ToolDefinition(
        name="get_logbook",
        description="House-wide activity log (automations triggered, devices changed state, scripts ran). Use this — NOT get_history — for 'what happened in the house?', 'what happened in the last hour?', 'did X run?', 'show recent activity'. Omit entity_id for house-wide; supply it to filter to one device. range: last_hour/last_24h/today/yesterday/last_7d/last_30d.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
