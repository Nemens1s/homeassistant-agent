from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register
from app.tools.helpers.timerange import TimeRange, resolve_range


class Params(BaseModel):
    entity_id: str = Field(description="Full entity id, e.g. 'sensor.living_room_temperature'. Must be a specific entity — wildcards not supported.")
    range: TimeRange = Field(
        default="last_24h",
        description="How far back to look. 'today' = since midnight local time. 'last_24h' = rolling 24-hour window. Other options: last_hour, yesterday, last_7d, last_30d.",
    )


async def handler(params: Params, ctx) -> ToolResult:
    if not params.entity_id or params.entity_id in ("*", "all") or params.entity_id.endswith(".*"):
        return ToolResult.error(
            "invalid_entity_id",
            "entity_id must be a specific entity (e.g. 'sensor.living_room_temperature'). "
            "For cross-entity activity use get_logbook instead.",
        )
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
        description="NOT for general activity — use get_logbook for 'what happened?' questions. This tool only gives the state-change timeline for ONE specific entity (e.g. temperature trend, battery %). entity_id must be a real entity id — wildcards rejected. 'today' = since midnight, 'last_24h' = rolling window.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
