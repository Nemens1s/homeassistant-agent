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
    device: str = Field(
        default="", description="Hardware device name (substring match). Returns all non-diagnostic entities for that device. Use list_devices to find device names."
    )
    domain: Domain = Field(
        default="", description="Optional domain filter, e.g. 'light', 'sensor', 'automation'."
    )
    device_class: DeviceClass = Field(
        default="", description="Optional device class filter, e.g. 'battery', 'motion', 'temperature', 'humidity'."
    )
    state: State = Field(
        default="", description="Optional state filter, e.g. 'on', 'off', 'unavailable', 'home', 'not_home'."
    )


def _device_name(d: dict) -> str:
    return d.get("name_by_user") or d.get("name") or d["id"]


async def _entity_ids_for_device(ctx, device_name: str) -> tuple[set[str] | None, list[str]]:
    """Return (entity_id_set, did_you_mean). entity_id_set is None when device not found."""
    devices = await ctx.ws.request_cached("config/device_registry/list")
    q = device_name.lower()
    match = next((d for d in devices if q in _device_name(d).lower()), None)
    if match is None:
        return None, [_device_name(d) for d in devices]
    entries = await ctx.ws.request_cached("config/entity_registry/list")
    ids = {
        e["entity_id"] for e in entries
        if e.get("device_id") == match["id"]
        and e.get("entity_category") not in ("diagnostic", "config")
    }
    return ids, []


async def handler(params: Params, ctx) -> ToolResult:
    if params.device:
        if ctx.ws is None:
            return ToolResult.error(
                "ws_unavailable",
                "Device filtering needs the websocket connection.",
            )
        entity_ids, suggestions = await _entity_ids_for_device(ctx, params.device)
        if entity_ids is None:
            return ToolResult.error(
                "device_not_found",
                f"No device matching {params.device!r}.",
                data={"did_you_mean": suggestions[:5]},
            )
        states = await ctx.rest.list_states()
        states = [s for s in states if s["entity_id"] in entity_ids]
    else:
        states = await ctx.rest.list_states()

    if params.domain:
        states = [s for s in states if s["entity_id"].startswith(params.domain + ".")]
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
        description=(
            "List HA entities with entity_ids and live states. "
            "Primary use: device='Roborock Qrevo S' → all non-diagnostic entities for that device "
            "(use list_devices to find device names). "
            "Also supports cross-device filters: domain='light', state='on', device_class='battery'. "
            "Not when entity_id already known — use get_entity_state."
        ),
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
