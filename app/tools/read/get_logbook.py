from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register
from app.tools.timerange import TimeRange, resolve_range


class Params(BaseModel):
    range: TimeRange = Field(
        default="last_24h",
        description="How far back to look: last_hour, last_24h, today, yesterday, last_7d, or last_30d.",
    )


async def handler(params: Params, ctx) -> ToolResult:
    start_time, end_time = resolve_range(params.range)
    entries = await ctx.rest.get_logbook(start_time, end_time)
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
        description="Get a time-range log of all events, state changes, and triggered automations across all entities, over a relative window (range=last_hour/last_24h/today/yesterday/last_7d/last_30d). Use for 'what happened in the house?', 'did automation X run today?', 'when did Y last occur?'. For state history of one specific entity only, use get_history instead.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
