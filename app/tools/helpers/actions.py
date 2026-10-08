"""Checks that an action may be run by the agent: the single source for
trigger_action (acting now) and the schedule tools (acting later)."""

from __future__ import annotations

from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX
from app.needle.menu import fields_to_parameters
from app.tools.base import ToolResult, entity_domain


async def check_action(ctx, entity_id: str, params: dict) -> ToolResult | None:
    """None when *entity_id* with *params* may be triggered, else the error."""
    if entity_id == ctx.settings.notify_action:
        return ToolResult.error(
            "use_notify_user", "Send notifications with notify_user, not trigger_action."
        )
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
        if params:
            return ToolResult.error("invalid_params", "automations do not take parameters.")
        return None
    return await _validate_script_params(ctx, entity_id.split(".", 1)[1], params)


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
