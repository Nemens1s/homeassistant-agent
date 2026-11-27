from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(
        description="Full entity id, e.g. 'light.kitchen' or 'sensor.living_room_temperature'"
    )


# Always returned when present; up to _MAX_EXTRA_ATTRIBUTES others follow.
_KEPT_ATTRIBUTES = ("friendly_name", "unit_of_measurement", "device_class")
_MAX_EXTRA_ATTRIBUTES = 12
_MAX_LIST_ITEMS = 5


def _is_nested(value: dict) -> bool:
    for inner in value.values():
        if isinstance(inner, (dict, list)):
            return True
    return False


def _compact(value):
    """A long list or a nested dict (e.g. a weather forecast) becomes a
    placeholder; returns (value, was_replaced)."""
    if isinstance(value, list) and len(value) > _MAX_LIST_ITEMS:
        return f"<{len(value)} items>", True
    if isinstance(value, dict) and _is_nested(value):
        return f"<{len(value)} items>", True
    return value, False


def cap_attributes(attributes: dict) -> tuple[dict, bool]:
    """Bound what one entity costs the model. Returns (attributes, truncated)."""
    capped = {}
    truncated = False
    for key in _KEPT_ATTRIBUTES:
        if key in attributes:
            capped[key] = attributes[key]
    extra = 0
    for key, value in attributes.items():
        if key in capped:
            continue
        if extra >= _MAX_EXTRA_ATTRIBUTES:
            truncated = True
            break
        value, replaced = _compact(value)
        if replaced:
            truncated = True
        capped[key] = value
        extra += 1
    return capped, truncated


async def handler(params: Params, ctx) -> ToolResult:
    state = await ctx.rest.get_state(params.entity_id)
    attributes, truncated = cap_attributes(state.get("attributes") or {})
    data = {
        "entity_id": state["entity_id"],
        "state": state["state"],
        "attributes": attributes,
        "last_changed": state.get("last_changed"),
    }
    if truncated:
        data["attributes_truncated"] = True
    return ToolResult.ok(data)


register(
    ToolDefinition(
        name="get_entity_state",
        description="Get the current state and attributes of one HA entity by its exact entity_id. Use ONLY when the exact entity_id is already known (e.g. given verbatim in the user's message). A friendly name or keyword is NOT an entity_id — it must be resolved to one first.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
