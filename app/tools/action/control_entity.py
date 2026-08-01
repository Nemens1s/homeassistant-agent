from typing import Literal

from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(description="Full entity id to control, e.g. 'light.kitchen'")
    action: Literal["turn_on", "turn_off", "toggle"] = Field(
        description="What to do with the entity"
    )


async def handler(params: Params, ctx) -> ToolResult:
    domain = params.entity_id.split(".", 1)[0]
    if domain not in ctx.settings.allowed_domains:
        return ToolResult.error(
            "domain_not_allowed",
            f"Domain {domain!r} is not in the allowed list.",
            data={"allowed": list(ctx.settings.allowed_domains)},
        )
    await ctx.rest.call_service(domain, params.action, params.entity_id)
    state = await ctx.rest.get_state(params.entity_id)
    return ToolResult.ok(
        {
            "entity_id": params.entity_id,
            "action": params.action,
            "state": state["state"],
            "name": state.get("attributes", {}).get("friendly_name", ""),
        }
    )


register(
    ToolDefinition(
        name="control_entity",
        description="Turn an entity on/off or toggle it (allowed domains only, e.g. lights and switches). Returns the entity's state after the action so you can confirm the outcome.",
        params_model=Params,
        tier=Tier.ACTION,
        handler=handler,
    )
)
