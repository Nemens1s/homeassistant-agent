from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    name_map = ctx.settings.person_name_lookup  # {ha_name: canonical_name}
    exclude = {s.lower() for s in ctx.settings.person_name_exclude}

    # Build reverse: canonical_name → [all ha_names that alias it]
    aliases_for: dict[str, list[str]] = {}
    for ha_name, canonical in name_map.items():
        aliases_for.setdefault(canonical, []).append(ha_name)

    rows = []
    for s in states:
        if not s["entity_id"].startswith("person."):
            continue
        ha_name = s.get("attributes", {}).get("friendly_name", s["entity_id"])
        canonical = name_map.get(ha_name, ha_name)
        if any(ex in canonical.lower() for ex in exclude):
            continue
        # Aliases: all ha_names that point to this canonical name, minus the one HA reported
        aliases = [a for a in aliases_for.get(canonical, []) if a != ha_name]
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
