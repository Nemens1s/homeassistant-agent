"""
Tools exposed to the LLM. Every one of these is read-only by construction:
there is no tool here that calls a service, sets a state, or otherwise
mutates Home Assistant. If you're tempted to add one, add it to a
*separate* tool list and a separate agent instance instead of here.
"""

from langchain_core.tools import tool
from . import ha_client


@tool
async def get_entity_state(entity_id: str) -> str:
    """Get the current state and attributes of a single Home Assistant entity.
    Use the full entity_id, e.g. 'sensor.living_room_temperature' or 'light.kitchen'."""
    data = await ha_client.get_state(entity_id)
    return str(data)


@tool
async def list_entities(domain: str = "") -> str:
    """List entities and their current state, optionally filtered by domain
    (e.g. 'sensor', 'light', 'binary_sensor', 'lock'). Leave domain empty to
    list everything (can be a large response for big installs)."""
    states = await ha_client.list_states()
    if domain:
        states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]
    lines = [
        f"{s['entity_id']}: {s['state']} ({s.get('attributes', {}).get('friendly_name', '')})"
        for s in states
    ]
    return "\n".join(lines) if lines else "No matching entities found."


@tool
async def get_entity_history(entity_id: str, start_time: str, end_time: str = "") -> str:
    """Get historical state changes for an entity between two ISO8601 timestamps.
    Example start_time: '2026-07-01T00:00:00'. end_time is optional (defaults to now)."""
    data = await ha_client.get_history(entity_id, start_time, end_time or None)
    return str(data)


@tool
async def get_recent_logbook(start_time: str, end_time: str = "") -> str:
    """Get logbook entries (state changes, triggered automations, etc.) since
    start_time (ISO8601 timestamp). end_time is optional (defaults to now)."""
    data = await ha_client.get_logbook(start_time, end_time or None)
    return str(data)


@tool
async def get_home_assistant_error_log() -> str:
    """Get the tail of Home Assistant's error log. Useful for answering
    'why isn't X working' style questions."""
    return await ha_client.get_error_log()


READ_ONLY_TOOLS = [
    get_entity_state,
    list_entities,
    get_entity_history,
    get_recent_logbook,
    get_home_assistant_error_log,
]
