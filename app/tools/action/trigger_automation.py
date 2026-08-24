from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register

# The AI menu marker. An automation is triggerable by the agent only when its
# entity_id starts with this prefix. Single source of truth (imported by
# get_automations for the ai_controllable flag).
AI_AUTOMATION_PREFIX = "automation.ai_"


class Params(BaseModel):
    entity_id: str = Field(
        description="AI-controllable automation entity id, e.g. 'automation.ai_night_lights'"
    )


async def handler(params: Params, ctx) -> ToolResult:
    if not params.entity_id.startswith("automation."):
        return ToolResult.error(
            "invalid_params", "entity_id must start with 'automation.'"
        )
    if not params.entity_id.startswith(AI_AUTOMATION_PREFIX):
        return ToolResult.error(
            "not_ai_controllable",
            f"{params.entity_id!r} is not an AI-controllable automation. "
            f"Only automations whose entity_id starts with {AI_AUTOMATION_PREFIX!r} "
            "may be triggered.",
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
        description=(
            "Run an AI-controllable Home Assistant automation right now by its "
            "entity_id. Only automations whose entity_id starts with 'automation.ai_' "
            "may be triggered (see the ai_controllable flag from get_automations). "
            "Requires the home's AI-actions switch to be on."
        ),
        params_model=Params,
        tier=Tier.ACTION,
        handler=handler,
    )
)
