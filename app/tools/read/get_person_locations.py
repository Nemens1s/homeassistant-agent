from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.helpers.people import resolve_name
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    exclude = set()
    for s in ctx.settings.person_name_exclude:
        exclude.add(s.lower())

    rows = []
    for s in states:
        if not s["entity_id"].startswith("person."):
            continue
        ha_name = s.get("attributes", {}).get("friendly_name", s["entity_id"])
        canonical, aliases = resolve_name(ha_name, ctx.settings)
        canonical_lower = canonical.lower()
        excluded = False
        for ex in exclude:
            if ex in canonical_lower:
                excluded = True
                break
        if excluded:
            continue
        row: dict = {"entity_id": s["entity_id"], "name": canonical, "state": s["state"]}
        if aliases:
            row["aliases"] = aliases
        rows.append(row)
    return ToolResult.ok({"rows": rows})


register(
    ToolDefinition(
        name="get_person_locations",
        description=(
            "Get the home/away state of all household members. "
            "Use for any question about who is home, who is away, or where a specific person is. "
            "Each row includes 'aliases' when the person is also known by other names."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
