from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    start_time: str = Field(description="ISO8601 start, e.g. '2026-07-12T00:00:00'")
    end_time: str = Field(default="", description="ISO8601 end; empty means now")


async def handler(params: Params, ctx) -> ToolResult:
    entries = await ctx.rest.get_logbook(params.start_time, params.end_time or None)
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
        description="Get a time-range log of all events, state changes, and triggered automations across all entities. Use for 'what happened in the house?', 'did automation X run today?', 'when did Y last occur?'. For state history of one specific entity only, use get_history instead.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
