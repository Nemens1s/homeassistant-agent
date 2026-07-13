"""
Minimal read-only client for the Home Assistant REST API.

Deliberately exposes only GET-based endpoints. There is no turn_on /
call_service / set_state method anywhere in this file — that's the actual
enforcement mechanism for "read-only," not a config toggle.
"""

import os
import httpx
from dotenv import load_dotenv

load_dotenv()

HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://supervisor/core")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

_headers = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}


async def _get(path: str, params: dict | None = None):
    async with httpx.AsyncClient(base_url=HA_BASE_URL, headers=_headers, timeout=10) as client:
        resp = await client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


async def get_state(entity_id: str) -> dict:
    return await _get(f"/api/states/{entity_id}")


async def list_states() -> list[dict]:
    return await _get("/api/states")


async def get_history(entity_id: str, start_time: str, end_time: str | None = None) -> list:
    params: dict = {"filter_entity_id": entity_id}
    if end_time:
        params["end_time"] = end_time
    return await _get(f"/api/history/period/{start_time}", params=params)


async def get_logbook(start_time: str, end_time: str | None = None) -> list:
    params: dict = {}
    if end_time:
        params["end_time"] = end_time
    return await _get(f"/api/logbook/{start_time}", params=params)


async def get_error_log() -> str:
    async with httpx.AsyncClient(base_url=HA_BASE_URL, headers=_headers, timeout=10) as client:
        resp = await client.get("/api/error_log")
        resp.raise_for_status()
        return resp.text
