from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    id: int = Field(description="The task id from list_scheduled.")


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.notes is None:
        return ToolResult.error("notes_unavailable", "The note store is unavailable.")
    if not ctx.notes.cancel(params.id):
        return ToolResult.error("not_found", f"No pending task with id {params.id}.")
    return ToolResult.ok({"id": params.id, "cancelled": True})


register(
    ToolDefinition(
        name="cancel_scheduled",
        description=(
            "Cancel a pending scheduled task by id (from list_scheduled) - e.g. 'never mind, "
            "let the vacuum run'."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
