"""HTTP backend: delegates classify() to a remote needle inference server.

The remote server owns the needle runtime and the model weights; this backend
sends a JSON request and maps the response back to a Decision. The router's
try/except handles any network error and degrades to the agent.
"""

from __future__ import annotations

import httpx

from app.needle.backend import Decision, _build_name_map, _decision_from_result
from app.needle.menu import Menu


class RemoteNeedleBackend:
    def __init__(self, base_url: str, timeout: float = 3.0):
        self._url = base_url.rstrip("/") + "/classify"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def classify(self, message: str, menu: Menu) -> Decision:
        name_to_id = _build_name_map(menu)
        id_to_desc = {item.entity_id: item.description or item.name for item in menu.items}
        tools = [
            {"name": name, "description": id_to_desc[entity_id]}
            for name, entity_id in name_to_id.items()
        ]
        payload = {"message": message, "tools": tools, "signature": menu.signature}
        response = await self._client.post(self._url, json=payload)
        print(f"Needle response {response}")
        response.raise_for_status()
        return _decision_from_result(response.json(), name_to_id)
