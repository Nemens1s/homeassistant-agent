from typing import Literal

from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register

# Constrained choices so a small model picks from a menu instead of inventing a
# value. "" means "no filter". Uncommon values are intentionally omitted — extend
# these lists if a real query needs one (a rejected value returns invalid_params).
Domain = Literal[
    "", "light", "switch", "sensor", "binary_sensor", "climate", "cover", "fan",
    "lock", "media_player", "automation", "script", "scene", "person",
    "device_tracker", "vacuum", "weather", "camera", "input_boolean", "number",
    "select", "button",
]
DeviceClass = Literal[
    "", "battery", "motion", "occupancy", "temperature", "humidity", "door",
    "window", "opening", "power", "energy", "illuminance", "pressure",
    "connectivity", "problem", "smoke", "moisture", "gas", "plug", "running",
    "tamper", "update",
]
State = Literal[
    "", "on", "off", "unavailable", "unknown", "home", "not_home", "open",
    "closed", "locked", "unlocked", "idle", "playing", "paused", "cleaning",
    "returning", "docked", "charging", "heat", "cool", "auto", "active",
    "standby", "detected", "clear",
]


class Params(BaseModel):
    domain: Domain = Field(
        default="", description="Optional domain filter, e.g. 'light', 'sensor', 'automation'."
    )
    area: str = Field(
        default="", description="Optional area name filter, e.g. 'Living room'."
    )
    device_class: DeviceClass = Field(
        default="", description="Optional device class filter, e.g. 'battery', 'motion', 'temperature', 'humidity'."
    )
    state: State = Field(
        default="", description="Optional state filter, e.g. 'on', 'off', 'unavailable', 'home', 'not_home'."
    )


async def _entity_ids_in_area(ctx, area_name: str) -> set[str] | None:
    areas = await ctx.ws.request_cached("config/area_registry/list")
    match = next((a for a in areas if a["name"].lower() == area_name.lower()), None)
    if match is None:
        return None
    area_id = match["area_id"]
    devices = await ctx.ws.request_cached("config/device_registry/list")
    device_ids = {d["id"] for d in devices if d.get("area_id") == area_id}
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    ids: set[str] = set()
    for e in entities:
        # entity's own area assignment overrides its device's area
        if e.get("area_id") == area_id or (
            e.get("area_id") is None and e.get("device_id") in device_ids
        ):
            ids.add(e["entity_id"])
    return ids


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    if params.domain:
        states = [s for s in states if s["entity_id"].startswith(params.domain + ".")]
    if params.area:
        if ctx.ws is None:
            return ToolResult.error(
                "ws_unavailable",
                "Area filtering needs the websocket connection, which is not available. Filter by domain instead.",
            )
        entity_ids = await _entity_ids_in_area(ctx, params.area)
        if entity_ids is None:
            areas = await ctx.ws.request_cached("config/area_registry/list")
            return ToolResult.error(
                "area_not_found",
                f"No area named {params.area!r}.",
                data={"available_areas": [a["name"] for a in areas]},
            )
        states = [s for s in states if s["entity_id"] in entity_ids]
    if params.device_class:
        states = [s for s in states if s.get("attributes", {}).get("device_class") == params.device_class]
    if params.state:
        states = [s for s in states if s["state"] == params.state]
    rows = [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "name": s.get("attributes", {}).get("friendly_name", ""),
        }
        for s in states
    ]
    return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))


register(
    ToolDefinition(
        name="list_entities",
        description="List entities WITH their current state, filtered by domain (domain='light' for all lights, 'switch', 'sensor', 'input_boolean'), area, device_class, or state."
                    " Use area='Living room' to see what entities are in a room and whether they're on/off — this is the tool for 'what entities/what's on in <room>'."
                    " Do NOT use when a full entity_id like 'light.kitchen' is already given (use get_entity_state) or when searching by device name (use search_entities).",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
