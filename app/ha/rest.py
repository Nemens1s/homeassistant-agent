"""GET-only client for the Home Assistant REST API.

Iteration 1 is read-only by construction: there is deliberately no
call_service / POST method in this class. Iteration 2 adds exactly one,
guarded by the domain allowlist.
"""

from __future__ import annotations

import httpx


class RestClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str, params: dict | None = None):
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def ping(self) -> bool:
        resp = await self._client.get("/api/")
        resp.raise_for_status()
        return True

    async def get_state(self, entity_id: str) -> dict:
        return await self._get_json(f"/api/states/{entity_id}")

    async def list_states(self) -> list[dict]:
        return await self._get_json("/api/states")

    async def get_history(
        self, entity_id: str, start_time: str, end_time: str | None = None
    ) -> list:
        params: dict = {"filter_entity_id": entity_id}
        if end_time:
            params["end_time"] = end_time
        return await self._get_json(f"/api/history/period/{start_time}", params)

    async def get_logbook(self, start_time: str, end_time: str | None = None) -> list:
        params: dict = {}
        if end_time:
            params["end_time"] = end_time
        return await self._get_json(f"/api/logbook/{start_time}", params)

    async def get_error_log(self) -> str:
        resp = await self._client.get("/api/error_log")
        resp.raise_for_status()
        return resp.text

    async def get_automation_config(self, automation_id: str) -> dict:
        return await self._get_json(f"/api/config/automation/config/{automation_id}")
