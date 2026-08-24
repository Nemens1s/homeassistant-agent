from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(
        description="Full entity id, e.g. 'light.kitchen' or 'sensor.living_room_temperature'"
    )


async def handler(params: Params, ctx) -> ToolResult:
    state = await ctx.rest.get_state(params.entity_id)
    return ToolResult.ok(
        {
            "entity_id": state["entity_id"],
            "state": state["state"],
            "attributes": state.get("attributes", {}),
            "last_changed": state.get("last_changed"),
        }
    )


register(
    ToolDefinition(
        name="get_entity_state",
        description="Get the current state and attributes of one HA entity by its exact entity_id. Use ONLY when the exact entity_id is already known (e.g. given verbatim in the user's message). A friendly name or keyword is NOT an entity_id — it must be resolved to one first.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
