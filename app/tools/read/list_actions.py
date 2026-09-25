import httpx
from pydantic import BaseModel

from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX
from app.needle.menu import fields_to_parameters
from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    rows = []
    for s in states:
        entity_id = s["entity_id"]
        if not entity_id.startswith((AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX)):
            continue
        if entity_id.startswith("automation.") and s["state"] == "off":
            continue
        row = {
            "entity_id": entity_id,
            "name": s.get("attributes", {}).get("friendly_name", ""),
        }
        if entity_id.startswith("script."):
            try:
                cfg = await ctx.rest.get_script_config(entity_id.split(".", 1)[1])
                params_schema = fields_to_parameters(cfg.get("fields") or {})
            except (httpx.HTTPStatusError, httpx.RequestError):
                params_schema = {"type": "object", "properties": {}}
            if params_schema.get("properties"):
                row["params"] = params_schema
        rows.append(row)
    return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))


register(
    ToolDefinition(
        name="list_actions",
        description="List the actions the agent may trigger right now (AI-controllable automations and scripts). Scripts include a 'params' schema of accepted arguments; pass matching values to trigger_action. Returns entity_id, name, state and (for scripts) params. Not for browsing all automations (use get_automations).",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
