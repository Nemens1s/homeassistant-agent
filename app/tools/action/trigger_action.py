from typing import Any

from pydantic import BaseModel, Field

from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX
from app.needle.menu import fields_to_parameters
from app.tools.base import Tier, ToolDefinition, ToolResult, entity_domain
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
    domain = entity_domain(entity_id)

    if domain not in ("automation", "script"):
        return ToolResult.error(
            "invalid_params", "entity_id must start with 'automation.' or 'script.'"
        )

    ai_prefix = AI_AUTOMATION_PREFIX if domain == "automation" else AI_SCRIPT_PREFIX
    if not entity_id.startswith(ai_prefix):
        return ToolResult.error(
            "not_ai_controllable",
            f"{entity_id!r} is not an AI-controllable {domain}. Only "
            f"{domain}s whose entity_id starts with {ai_prefix!r} may be triggered.",
        )

    if domain not in ctx.settings.allowed_domains:
        return ToolResult.error(
            "domain_not_allowed",
            f"The {domain!r} domain is not in the allowed list.",
            data={"allowed": list(ctx.settings.allowed_domains)},
        )

    if domain == "automation":
        if params.params:
            return ToolResult.error(
                "invalid_params", "automations do not take parameters."
            )
        await ctx.rest.call_service("automation", "trigger", entity_id)
    else:
        object_id = entity_id.split(".", 1)[1]
        error = await _validate_script_params(ctx, object_id, params.params)
        if error is not None:
            return error
        await ctx.rest.call_service("script", object_id, data=params.params)

    state = await ctx.rest.get_state(entity_id)
    return ToolResult.ok({
        "entity_id": entity_id,
        "triggered": True,
        "last_triggered": state.get("attributes", {}).get("last_triggered"),
    })


async def _validate_script_params(ctx, object_id: str, provided: dict):
    try:
        cfg = await ctx.rest.get_script_config(object_id)
    except Exception:
        return None  # can't validate -> let the script's guards handle it
    if cfg is None:
        return None
    schema = fields_to_parameters(cfg.get("fields") or {})
    declared = set(schema.get("properties", {}))
    unknown = set(provided) - declared
    if unknown:
        return ToolResult.error(
            "invalid_params", f"unknown parameter(s): {', '.join(sorted(unknown))}")
    missing = set(schema.get("required", [])) - set(provided)
    if missing:
        return ToolResult.error(
            "invalid_params", f"missing required parameter(s): {', '.join(sorted(missing))}")
    return None


register(
    ToolDefinition(
        name="trigger_action",
        description=(
            "Run an AI-controllable Home Assistant action by entity_id. "
            "Automations (automation.ai_*) take no params; scripts (script.ai_*) "
            "take the arguments shown in the 'params' schema from list_actions. "
            "Requires the home's AI-actions switch to be on."
        ),
        params_model=Params,
        tier=Tier.ACTION,
        handler=handler,
    )
)
