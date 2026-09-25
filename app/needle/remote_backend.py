"""HTTP backend: delegates classify() to a remote needle inference server.

The remote server owns the needle runtime and the model weights; this backend
sends a JSON request and maps the response back to a Decision. The router's
try/except handles any network error and degrades to the agent.
"""

from __future__ import annotations

import httpx

from app.fast_path.backend import Decision
from app.needle.backend import _build_name_map, _decision_from_result
from app.needle.menu import Menu


class RemoteNeedleBackend:
    name = "needle-remote"

    def __init__(self, base_url: str, timeout: float = 3.0):
        self._url = base_url.rstrip("/") + "/classify"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def classify(self, message: str, menu: Menu) -> Decision:
        name_to_id = _build_name_map(menu)
        id_to_item = {item.entity_id: item for item in menu.items}
        tools = []
        for name, entity_id in name_to_id.items():
            item = id_to_item[entity_id]
            tools.append({
                "name": name,
                "description": item.description or item.name,
                "parameters": item.parameters or {},
            })
        print(f"Tools {tools}")
        payload = {"message": message, "tools": tools, "signature": menu.signature}
        response = await self._client.post(self._url, json=payload)
        response.raise_for_status()
        return _decision_from_result(response.json(), name_to_id)
