"""/api/actions: the ai_* menu and its enable/disable toggle (settings UI)."""

from __future__ import annotations

from contextlib import contextmanager

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX
from app.ha.websocket import ws_is_ready

router = APIRouter()


class ToggleRequest(BaseModel):
    enabled: bool


@contextmanager
def _ha_errors_as_http():
    """Turn a failed HA call into a 503 with a readable detail (the settings
    UI shows it) instead of an unhandled 500."""
    try:
        yield
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Home Assistant did not answer in time")
    except (ConnectionError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=503, detail=f"Could not reach Home Assistant: {exc}")
    except RuntimeError as exc:
        # The websocket raises RuntimeError when HA answers success=False.
        raise HTTPException(status_code=503, detail=f"Home Assistant refused the command: {exc}")


@router.get("/api/actions")
async def get_actions(request: Request) -> dict:
    state = request.app.state
    ws = state.ws
    if not ws_is_ready(ws):
        raise HTTPException(status_code=503, detail="websocket unavailable")

    with _ha_errors_as_http():
        registry_entries = await ws.request("config/entity_registry/list")
        states = await state.rest.list_states()
    state_map = {s["entity_id"]: s for s in states}

    actions = []
    for entry in registry_entries:
        entity_id = entry["entity_id"]
        if not entity_id.startswith((AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX)):
            continue

        disabled_by = entry.get("disabled_by")
        is_automation = entity_id.startswith("automation.")

        if is_automation:
            s = state_map.get(entity_id)
            enabled = (not disabled_by) and s is not None and s["state"] == "on"
        else:
            enabled = disabled_by is None

        s = state_map.get(entity_id)
        if s:
            name = s.get("attributes", {}).get("friendly_name") or ""
        else:
            name = entry.get("name") or entry.get("original_name") or entity_id

        actions.append({
            "entity_id": entity_id,
            "name": name,
            "type": "automation" if is_automation else "script",
            "enabled": enabled,
        })

    return {"actions": actions}


@router.post("/api/actions/{entity_id:path}/toggle")
async def toggle_action(entity_id: str, req: ToggleRequest, request: Request) -> dict:
    if not entity_id.startswith((AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX)):
        raise HTTPException(status_code=403, detail="not an AI-controlled entity")

    ws = request.app.state.ws
    if not ws_is_ready(ws):
        raise HTTPException(status_code=503, detail="websocket unavailable")

    with _ha_errors_as_http():
        if entity_id.startswith("automation."):
            service = "turn_on" if req.enabled else "turn_off"
            await ws.management_request(
                "call_service",
                domain="automation",
                service=service,
                service_data={"entity_id": entity_id},
            )
        else:
            await ws.management_request(
                "config/entity_registry/update",
                entity_id=entity_id,
                disabled_by=None if req.enabled else "user",
            )
            if req.enabled:
                await ws.management_request("call_service", domain="script", service="reload")

    return {"ok": True}
