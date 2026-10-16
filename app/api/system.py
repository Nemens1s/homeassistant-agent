"""/api/health, /api/prompt and /api/history."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.agent.factory import build_system_prompt
from app.agent.history import display_transcript
from app.health import UNKNOWN
from app.i18n.store import apply_overlay

router = APIRouter()


@router.get("/api/health")
async def health(request: Request) -> dict:
    # Always HTTP 200, even when everything is down. The Supervisor watchdog
    # polls this endpoint and restarts the add-on on any other status, so a
    # 503 during an LLM outage would restart-loop the add-on while the
    # degraded mode is the thing that should be answering. The body carries
    # the real state; the trackers are read here, never probed.
    state = request.app.state
    ha_ok = False
    try:
        ha_ok = await state.rest.ping()
    except Exception:
        pass
    llm_health = getattr(state, "llm_health", None)
    if llm_health is not None:
        llm_snap = llm_health.snapshot()
    else:
        llm_snap = {"state": UNKNOWN, "since": None, "error": None}
    llm_ok = llm_snap["state"] == "up"
    fp_health = getattr(state, "fp_health", None)
    fast_path_state = fp_health.state if fp_health is not None else None
    ws_ok = state.ws is not None and state.ws.connected
    return {
        "status": "ok" if (ha_ok and llm_ok) else "degraded",
        "ha": ha_ok,
        "llm": llm_ok,
        "llm_state": llm_snap["state"],
        "llm_since": llm_snap["since"],
        "llm_error": llm_snap["error"],
        "provider": state.settings.llm_provider,
        "fast_path_state": fast_path_state,
        "websocket": ws_ok,
    }


@router.get("/api/prompt")
async def get_prompt(request: Request) -> dict:
    state = request.app.state
    return {"prompt": build_system_prompt(state.settings, state.ctx.skills_dir)}


@router.get("/api/history")
async def get_history(thread_id: str, request: Request) -> dict:
    state = request.app.state
    snap = await state.agent.aget_state({"configurable": {"thread_id": thread_id}})
    messages = (snap.values or {}).get("messages", [])
    turns = display_transcript(messages)
    overlay = getattr(state, "lang_overlay", None)
    if overlay is not None:
        turns = apply_overlay(turns, overlay.rows_for(thread_id))
    return {"messages": turns}
