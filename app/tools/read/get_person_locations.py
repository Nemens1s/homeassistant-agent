from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    rows = [
        {
            "entity_id": s["entity_id"],
            "name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
            "state": s["state"],
        }
        for s in states
        if s["entity_id"].startswith("person.")
    ]
    return ToolResult.ok({"rows": rows})


register(
    ToolDefinition(
        name="get_person_locations",
        description=(
            "Get the home/away state of all household members. "
            "Use for any question about who is home, who is away, or where a specific person is."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
