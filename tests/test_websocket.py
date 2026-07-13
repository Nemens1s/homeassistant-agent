import asyncio
import json
import logging

import pytest
import websockets

from app.ha.websocket import WebSocketClient

AREAS = [{"area_id": "living", "name": "Living room"}]


async def _fake_ha(ws):
    await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.7"}))
    msg = json.loads(await ws.recv())
    if msg.get("access_token") != "secret":
        await ws.send(json.dumps({"type": "auth_invalid", "message": "bad token"}))
        return
    await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.7"}))
    async for raw in ws:
        msg = json.loads(raw)
        if msg["type"] == "config/area_registry/list":
            await ws.send(
                json.dumps(
                    {"id": msg["id"], "type": "result", "success": True, "result": AREAS}
                )
            )
        else:
            await ws.send(
                json.dumps(
                    {
                        "id": msg["id"],
                        "type": "result",
                        "success": False,
                        "error": {"code": "unknown_command", "message": "unknown"},
                    }
                )
            )


@pytest.fixture
async def server_url():
    async with websockets.serve(_fake_ha, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def test_auth_and_request(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    assert client.connected
    result = await client.request("config/area_registry/list")
    assert result == AREAS
    await client.stop()


async def test_bad_token_fails_start(server_url):
    client = WebSocketClient(server_url, "wrong")
    with pytest.raises(TimeoutError):
        await client.start(connect_timeout=1)
    await client.stop()


async def test_error_response_raises(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    with pytest.raises(RuntimeError):
        await client.request("no/such/command")
    await client.stop()


async def test_request_cached_hits_cache(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    a = await client.request_cached("config/area_registry/list")
    b = await client.request_cached("config/area_registry/list")
    assert a == b == AREAS
    # ids increment only once: second call served from cache
    assert client._next_id == 2
    await client.stop()


async def test_clean_stop_no_warnings(server_url, caplog):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    with caplog.at_level(logging.WARNING, logger="agent.ws"):
        await client.stop()
    warnings = [
        r for r in caplog.records
        if r.name == "agent.ws" and r.levelno >= logging.WARNING
    ]
    assert warnings == []
