from datetime import datetime, timezone

from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.helpers.notes import note_summary
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.notes is None:
        return ToolResult.error("notes_unavailable", "The note store is unavailable.")
    rows = []
    for note in ctx.notes.list_pending(datetime.now(timezone.utc)):
        rows.append(note_summary(note))
    return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))


register(
    ToolDefinition(
        name="list_scheduled",
        description=(
            "List pending scheduled tasks (reminders and deferred actions) with "
            "their id, trigger and instruction. Use before cancel_scheduled, or when "
            "the user asks what reminders they have."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
