"""The agent's action menu: the automations (and, later, scripts) it may
trigger. Kept separate from get_automations so the model has a short, focused
list of *actionable* things rather than every automation in the home. The
entity_id prefix (automation./script.) already carries the domain, so rows
need no separate "type" field."""

from pydantic import BaseModel

from app.constants import AI_AUTOMATION_PREFIX
from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    rows = []
    for s in states:
        if not s["entity_id"].startswith(AI_AUTOMATION_PREFIX):
            continue
        rows.append({
            "entity_id": s["entity_id"],
            "state": s["state"],
            "name": s.get("attributes", {}).get("friendly_name", ""),
        })
    return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))


register(
    ToolDefinition(
        name="list_actions",
        description="List the actions the agent may trigger right now (AI-controllable automations). Use this to discover what you can run before calling trigger_action. Returns entity_id, name and current state. Not for browsing all automations (use get_automations) or checking whether one ran (activity history).",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
