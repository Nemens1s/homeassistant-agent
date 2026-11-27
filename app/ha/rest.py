"""REST client for the Home Assistant REST API.

Iteration 1 was read-only by construction. Iteration 2 adds exactly one
write method — call_service — guarded by a domain allowlist supplied at
construction time. An empty allowlist (the default) keeps the client
effectively read-only.
"""

from __future__ import annotations

import time

import httpx

# How long a script's config is reused. list_actions, check_action and the
# fast-path menu each read every ai_* script's config per turn; the menu
# already accepts this much staleness.
SCRIPT_CONFIG_TTL_S = 60.0


class RestClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        allowed_write_domains: tuple[str, ...] = (),
    ):
        self._allowed_write_domains = allowed_write_domains
        self._script_configs: dict[str, tuple[float, dict]] = {}
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

    async def get_logbook(
        self, start_time: str, end_time: str | None = None, entity_id: str | None = None
    ) -> list:
        params: dict = {}
        if end_time:
            params["end_time"] = end_time
        if entity_id:
            params["entity_id"] = entity_id
        return await self._get_json(f"/api/logbook/{start_time}", params)

    async def get_error_log(self) -> str:
        resp = await self._client.get("/api/error_log")
        resp.raise_for_status()
        return resp.text

    async def get_automation_config(self, automation_id: str) -> dict:
        return await self._get_json(f"/api/config/automation/config/{automation_id}")

    async def get_script_config(self, object_id: str) -> dict:
        """Cached for SCRIPT_CONFIG_TTL_S per object_id (like
        WebSocketClient.request_cached); failures are not cached."""
        hit = self._script_configs.get(object_id)
        if hit is not None and time.monotonic() - hit[0] < SCRIPT_CONFIG_TTL_S:
            return hit[1]
        config = await self._get_json(f"/api/config/script/config/{object_id}")
        self._script_configs[object_id] = (time.monotonic(), config)
        return config

    async def call_service(
        self, domain: str, service: str, entity_id: str | None = None,
        data: dict | None = None,
    ) -> list:
        """The ONLY write method. Refuses domains outside the allowlist the
        client was constructed with. `data` is the service payload (e.g. script
        field variables); `entity_id`, when given, is merged into it."""
        if domain not in self._allowed_write_domains:
            raise PermissionError(f"write domain not allowed: {domain!r}")
        body: dict = dict(data or {})
        if entity_id is not None:
            body["entity_id"] = entity_id
        resp = await self._client.post(
            f"/api/services/{domain}/{service}", json=body
        )
        resp.raise_for_status()
        return resp.json()
