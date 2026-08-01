from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    pass


_NAME_MAP = {
    "ilniko": "Ilja",
    "megakrasotka2002": "Sofija",
    "Sonja": "Sofija",
    "Sonya": "Sofija",
}
_EXCLUDE = {"ipad"}


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    rows = []
    for s in states:
        if not s["entity_id"].startswith("person."):
            continue
        name = s.get("attributes", {}).get("friendly_name", s["entity_id"])
        name = _NAME_MAP.get(name, name)
        if name.lower() in _EXCLUDE:
            continue
        rows.append({"entity_id": s["entity_id"], "name": name, "state": s["state"]})
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
