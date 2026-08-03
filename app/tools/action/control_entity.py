from typing import Literal

from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, entity_domain
from app.tools.labels import check_entity_labels
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(description="Full entity id to control, e.g. 'light.kitchen'")
    action: Literal["turn_on", "turn_off", "toggle"] = Field(
        description="What to do with the entity"
    )


async def handler(params: Params, ctx) -> ToolResult:
    domain = entity_domain(params.entity_id)
    if domain == "automation":
        return ToolResult.error(
            "wrong_tool",
            "Use trigger_automation to run an automation. "
            "turn_on/turn_off on automations only enables/disables them, it does not run them.",
        )
    if domain not in ctx.settings.allowed_domains:
        return ToolResult.error(
            "domain_not_allowed",
            f"Domain {domain!r} is not in the allowed list.",
            data={"allowed": list(ctx.settings.allowed_domains)},
        )
    label_ok = await check_entity_labels(params.entity_id, ctx.settings.allowed_labels, ctx)
    if label_ok is False:
        return ToolResult.error(
            "label_not_allowed",
            f"{params.entity_id!r} does not have any of the required labels: {ctx.settings.allowed_labels}.",
        )
    pre = await ctx.rest.get_state(params.entity_id)
    if pre["state"] == "unavailable":
        return ToolResult.error(
            "entity_unavailable",
            f"{params.entity_id!r} is unavailable — the device may be offline.",
        )
    await ctx.rest.call_service(domain, params.action, params.entity_id)
    return ToolResult.ok({"entity_id": params.entity_id, "action": params.action})


register(
    ToolDefinition(
        name="control_entity",
        description="Turn a light or switch on/off/toggle. Only use with light.* or switch.* entity_ids — never automation.* or sensor.*. Returns ok when HA accepted the command; state updates asynchronously so use get_entity_state to verify.",
        params_model=Params,
        tier=Tier.ACTION,
        handler=handler,
    )
)
