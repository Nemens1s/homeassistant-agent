from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    query: str = Field(description="Search term matched case-insensitively against entity_id and friendly name.")


async def _area_by_entity(ctx) -> dict[str, str]:
    """Return {entity_id: area_name} using WS registries."""
    areas = await ctx.ws.request_cached("config/area_registry/list")
    devices = await ctx.ws.request_cached("config/device_registry/list")
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    area_name = {a["area_id"]: a["name"] for a in areas}
    device_area = {d["id"]: d.get("area_id") for d in devices}
    result: dict[str, str] = {}
    for e in entities:
        area_id = e.get("area_id") or device_area.get(e.get("device_id"))
        if area_id and area_id in area_name:
            result[e["entity_id"]] = area_name[area_id]
    return result


async def handler(params: Params, ctx) -> ToolResult:
    words = params.query.lower().split()
    states = await ctx.rest.list_states()
    matches = [
        s for s in states
        if any(
            w in s["entity_id"].lower()
            or w in s.get("attributes", {}).get("friendly_name", "").lower()
            for w in words
        )
    ]
    area_map = await _area_by_entity(ctx) if ctx.ws is not None else {}
    rows = []
    for s in matches:
        row: dict = {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name", ""),
        }
        if ctx.ws is not None:
            row["area"] = area_map.get(s["entity_id"], "")
        rows.append(row)
    return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))


register(
    ToolDefinition(
        name="search_entities",
        description=(
            "Search entities by name or entity_id keyword. Returns state, friendly name, and area for each match. "
            "Use instead of get_areas_and_devices when looking for a specific device by name (e.g. 'roborock', 'thermostat')."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
