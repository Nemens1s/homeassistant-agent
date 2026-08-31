import difflib

from pydantic import BaseModel, Field

from app.constants import AI_AUTOMATION_PREFIX
from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(
        default="",
        description="Optional automation entity_id (e.g. 'automation.night_lights') to fetch the full config for. Empty lists all automations.",
    )
    ai_controllable: bool = Field(
        default=False,
        description="When true, list only AI-controllable automations (those the agent may trigger). Ignored when entity_id is set.",
    )


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    autos = []
    for s in states:
        if s["entity_id"].startswith("automation."):
            autos.append(s)

    if not params.entity_id:
        rows = []
        for a in autos:
            is_ai_controllable = a["entity_id"].startswith(AI_AUTOMATION_PREFIX)
            if params.ai_controllable and not is_ai_controllable:
                continue
            rows.append({
                "entity_id": a["entity_id"],
                "state": a["state"],
                "name": a.get("attributes", {}).get("friendly_name", ""),
                "last_triggered": a.get("attributes", {}).get("last_triggered"),
                "ai_controllable": is_ai_controllable,
            })
        return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))

    match = None
    for a in autos:
        if a["entity_id"] == params.entity_id:
            match = a
            break
    if match is None:
        ids = []
        for a in autos:
            ids.append(a["entity_id"])
        return ToolResult.error(
            "entity_not_found",
            f"No automation {params.entity_id!r}.",
            data={"did_you_mean": difflib.get_close_matches(params.entity_id, ids, n=3, cutoff=0.4)},
        )

    automation_id = match.get("attributes", {}).get("id")
    if automation_id is None:
        return ToolResult.ok(
            {
                "entity_id": match["entity_id"],
                "state": match["state"],
                "attributes": match.get("attributes", {}),
                "note": "YAML-defined automation; full config not retrievable via the API.",
            }
        )
    config = await ctx.rest.get_automation_config(automation_id)
    return ToolResult.ok(config)


register(
    ToolDefinition(
        name="get_automations",
        description="List all automations with enabled/disabled state (ai_controllable=true lists only ones the agent may trigger), or fetch one automation's full config (triggers, conditions, actions). Use for 'is automation X enabled?', 'what triggers X?', 'which automations run when X?'. Not for whether one ran recently (activity history) or diagnosing why one failed (troubleshooting playbook).",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
