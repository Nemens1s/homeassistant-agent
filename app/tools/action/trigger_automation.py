from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.labels import check_entity_labels
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(description="Automation entity id, e.g. 'automation.night_lights'")


async def handler(params: Params, ctx) -> ToolResult:
    if not params.entity_id.startswith("automation."):
        return ToolResult.error(
            "invalid_params", "entity_id must start with 'automation.'"
        )
    label_ok = await check_entity_labels(params.entity_id, ctx.settings.allowed_labels, ctx)
    if label_ok is False:
        return ToolResult.error(
            "label_not_allowed",
            f"{params.entity_id!r} does not have any of the required labels: {ctx.settings.allowed_labels}.",
        )
    if "automation" not in ctx.settings.allowed_domains:
        return ToolResult.error(
            "domain_not_allowed",
            "The 'automation' domain is not in the allowed list.",
            data={"allowed": list(ctx.settings.allowed_domains)},
        )
    await ctx.rest.call_service("automation", "trigger", params.entity_id)
    state = await ctx.rest.get_state(params.entity_id)
    return ToolResult.ok(
        {
            "entity_id": params.entity_id,
            "triggered": True,
            "last_triggered": state.get("attributes", {}).get("last_triggered"),
        }
    )


register(
    ToolDefinition(
        name="trigger_automation",
        description="Run a Home Assistant automation right now by its entity_id. Only works when the 'automation' domain is allowed.",
        params_model=Params,
        tier=Tier.ACTION,
        handler=handler,
    )
)
