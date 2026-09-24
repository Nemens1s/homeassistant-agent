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
        elif msg["type"] == "config/entity_registry/list":
            # no reply — simulates a hung command
            pass
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
        await client.request("config/device_registry/list")
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


async def test_request_rejects_non_readonly_command(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    with pytest.raises(PermissionError):
        await client.request("call_service", domain="light", service="turn_on")
    # no message id was consumed and no pending future leaked
    assert client._next_id == 1
    assert client._pending == {}
    await client.stop()


async def test_request_total_deadline_single_budget(server_url):
    import time as _time

    client = WebSocketClient(server_url, "secret", request_timeout=1.0)
    await client.start(connect_timeout=5)
    started = _time.monotonic()
    with pytest.raises(TimeoutError):
        # fake server answers unknown commands with an error; use a command
        # it silently ignores instead: add "config/entity_registry/list"
        # handling to _fake_ha that never replies (see Step 3 note below)
        await client.request("config/entity_registry/list")
    elapsed = _time.monotonic() - started
    assert elapsed < 1.5  # one budget, not connect-wait + response-wait
    assert client._pending == {}  # entry popped in finally
    await client.stop()


async def _fake_ha_management(ws):
    await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.7"}))
    msg = json.loads(await ws.recv())
    if msg.get("access_token") != "secret":
        await ws.send(json.dumps({"type": "auth_invalid", "message": "bad token"}))
        return
    await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.7"}))
    async for raw in ws:
        msg = json.loads(raw)
        if msg["type"] in ("call_service", "config/entity_registry/update"):
            await ws.send(json.dumps(
                {"id": msg["id"], "type": "result", "success": True, "result": None}
            ))
        else:
            await ws.send(json.dumps(
                {"id": msg["id"], "type": "result", "success": False,
                 "error": {"code": "unknown_command", "message": "unknown"}}
            ))


@pytest.fixture
async def management_server_url():
    async with websockets.serve(_fake_ha_management, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def test_management_request_allows_call_service(management_server_url):
    client = WebSocketClient(management_server_url, "secret")
    await client.start(connect_timeout=5)
    result = await client.management_request(
        "call_service", domain="automation", service="turn_on",
        service_data={"entity_id": "automation.ai_night_lights"},
    )
    assert result is None
    await client.stop()


async def test_management_request_allows_entity_registry_update(management_server_url):
    client = WebSocketClient(management_server_url, "secret")
    await client.start(connect_timeout=5)
    result = await client.management_request(
        "config/entity_registry/update",
        entity_id="script.ai_action_music",
        disabled_by="user",
    )
    assert result is None
    await client.stop()


async def test_management_request_rejects_non_management_command(management_server_url):
    client = WebSocketClient(management_server_url, "secret")
    await client.start(connect_timeout=5)
    with pytest.raises(PermissionError):
        await client.management_request("config/area_registry/list")
    assert client._next_id == 1
    assert client._pending == {}
    await client.stop()


async def test_request_still_rejects_call_service(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    with pytest.raises(PermissionError):
        await client.request("call_service", domain="automation", service="turn_on")
    await client.stop()


async def test_disconnect_clears_cache(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    await client.request_cached("config/area_registry/list")
    assert client._cache
    client._handle_disconnect(ConnectionError("test"))
    assert client._cache == {}
    assert not client.connected
    await client.stop()
