"""Resolves where a chat request came from: the requesting device's area and
the person speaking. The HTTP edge forwards these raw HA ids (Gosling passes
them from Assist); the answers reach the model as one context line.

Every function here fails open: an unknown id, a missing registry or an HA
error means "not known", never an exception, because a request must not
fail or stall over optional context. Not a tool module — registers nothing.
"""

from __future__ import annotations

import logging

from app.agent.run_scope import RequestFacts
from app.tools.context import ws_ready
from app.tools.helpers.lookups import area_names
from app.tools.helpers.people import resolve_name

log = logging.getLogger("agent")


async def device_area(ctx, device_id: str | None) -> tuple[str, str] | None:
    """(area_id, area_name) of the device, or None. Phones have no area."""
    if not device_id or not ws_ready(ctx):
        return None
    try:
        devices = await ctx.ws.request_cached("config/device_registry/list")
        area_id = None
        for device in devices:
            if device.get("id") == device_id:
                area_id = device.get("area_id")
                break
        if not area_id:
            return None
        names = await area_names(ctx)
    except Exception:  # fail open: see module docstring
        log.debug("request context: device area lookup failed", exc_info=True)
        return None
    name = names.get(area_id)
    if not name:
        return None
    return area_id, name


async def user_person(ctx, user_id: str | None) -> tuple[str, str] | None:
    """(person name, person entity_id) of the HA user, or None."""
    if not user_id:
        return None
    try:
        states = await ctx.rest.list_states()
    except Exception:  # fail open: see module docstring
        log.debug("request context: person lookup failed", exc_info=True)
        return None
    for state in states:
        entity_id = state.get("entity_id", "")
        if not entity_id.startswith("person."):
            continue
        attributes = state.get("attributes") or {}
        if attributes.get("user_id") != user_id:
            continue
        friendly_name = attributes.get("friendly_name") or entity_id
        canonical, _aliases = resolve_name(friendly_name, ctx.settings)
        return canonical, entity_id
    return None


async def resolve_request_facts(ctx, device_id: str | None, user_id: str | None) -> RequestFacts:
    """Everything that resolved; the rest is left None."""
    area = await device_area(ctx, device_id)
    person = await user_person(ctx, user_id)
    area_id = None
    area_name = None
    if area is not None:
        area_id, area_name = area
    person_name = None
    person_entity_id = None
    if person is not None:
        person_name, person_entity_id = person
    return RequestFacts(area_id, area_name, person_name, person_entity_id)
