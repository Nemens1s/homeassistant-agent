from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.actions import check_action
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(
        description="AI-controllable automation or script entity id, e.g. "
        "'automation.ai_night_lights' or 'script.ai_action_lights_on'."
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for a script action (see the 'params' schema from "
        "list_actions). Leave empty for automations.",
    )


async def handler(params: Params, ctx) -> ToolResult:
    entity_id = params.entity_id
    error = await check_action(ctx, entity_id, params.params)
    if error is not None:
        return error

    if entity_id.startswith("automation."):
        await ctx.rest.call_service("automation", "trigger", entity_id)
    else:
        object_id = entity_id.split(".", 1)[1]
        await ctx.rest.call_service("script", object_id, data=params.params)

    state = await ctx.rest.get_state(entity_id)
    return ToolResult.ok({
        "entity_id": entity_id,
        "triggered": True,
        "last_triggered": state.get("attributes", {}).get("last_triggered"),
    })


register(
    ToolDefinition(
        name="trigger_action",
        description=(
            "Run an AI-controllable Home Assistant action by entity_id. "
            "Automations (automation.ai_*) take no params; scripts (script.ai_*) "
            "take the arguments shown in the 'params' schema from list_actions."
        ),
        params_model=Params,
        tier=Tier.ACTION,
        handler=handler,
    )
)
