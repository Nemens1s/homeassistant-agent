import pytest
import httpx

from app.needle.menu import MenuItem, Menu, _signature
from app.needle.remote_backend import RemoteNeedleBackend


def _menu(items):
    return Menu(items=tuple(items), signature=_signature(list(items)))


@pytest.mark.asyncio
async def test_remote_backend_sends_parameters(monkeypatch):
    captured = {}

    async def fake_post(url, json):
        captured["payload"] = json
        return httpx.Response(200, json={"function_calls": [], "confidence": 0.0},
                              request=httpx.Request("POST", url))

    backend = RemoteNeedleBackend("http://needle.test")
    monkeypatch.setattr(backend._client, "post", fake_post)
    menu = _menu([MenuItem(
        entity_id="script.ai_action_lights_on", name="Lights On",
        description="turn on room lights.",
        parameters={"type": "object",
                    "properties": {"room": {"type": "string", "enum": ["living_room"]}},
                    "required": ["room"]})])
    await backend.classify("lights on", menu)
    tool = captured["payload"]["tools"][0]
    assert tool["parameters"]["properties"]["room"]["enum"] == ["living_room"]
