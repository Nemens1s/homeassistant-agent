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


SUBSCRIBE = {
    "type": "subscribe_trigger",
    "trigger": {"platform": "state", "entity_id": ["vacuum.x"], "to": None},
}


def _event_server(state):
    """Fake HA: acks each subscribe_trigger, then sends one event on its id.
    With state['drop_first'] it closes the connection after the first one."""

    async def handler(ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_ok"}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] != "subscribe_trigger":
                continue
            state["subscribes"].append(msg)
            await ws.send(json.dumps(
                {"id": msg["id"], "type": "result", "success": True, "result": None}
            ))
            await ws.send(json.dumps({
                "id": msg["id"],
                "type": "event",
                "event": {"variables": {"trigger": {"entity_id": "vacuum.x"}}},
            }))
            if state.get("drop_first") and len(state["subscribes"]) == 1:
                await ws.close()
                return

    return handler


@pytest.fixture
async def event_server():
    state = {"subscribes": []}
    async with websockets.serve(_event_server(state), "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}", state


async def test_subscribe_delivers_events(event_server):
    url, state = event_server
    received = asyncio.Queue()

    async def on_event(event):
        await received.put(event)

    client = WebSocketClient(url, "secret")
    await client.start(connect_timeout=5)
    await client.subscribe(SUBSCRIBE, on_event)
    event = await asyncio.wait_for(received.get(), timeout=5)
    assert event["variables"]["trigger"]["entity_id"] == "vacuum.x"
    assert state["subscribes"][0]["trigger"]["to"] is None
    await client.stop()


async def test_subscribe_before_connect_is_sent_on_connect(event_server):
    url, state = event_server
    received = asyncio.Queue()

    async def on_event(event):
        await received.put(event)

    client = WebSocketClient(url, "secret")
    await client.subscribe(SUBSCRIBE, on_event)  # not connected yet: only stored
    assert state["subscribes"] == []
    client.start_background()
    await asyncio.wait_for(received.get(), timeout=5)
    assert len(state["subscribes"]) == 1
    await client.stop()


async def test_subscription_is_resent_after_reconnect(event_server):
    url, state = event_server
    state["drop_first"] = True
    received = asyncio.Queue()

    async def on_event(event):
        await received.put(event)

    client = WebSocketClient(url, "secret")
    await client.start(connect_timeout=5)
    await client.subscribe(SUBSCRIBE, on_event)
    await asyncio.wait_for(received.get(), timeout=5)
    # second event arrives only if the client reconnected AND resubscribed
    await asyncio.wait_for(received.get(), timeout=10)
    assert len(state["subscribes"]) == 2
    assert state["subscribes"][1]["id"] != state["subscribes"][0]["id"]
    await client.stop()


async def test_subscribe_rejects_other_commands():
    client = WebSocketClient("ws://127.0.0.1:1", "secret")

    async def on_event(event):
        pass

    with pytest.raises(PermissionError):
        await client.subscribe({"type": "subscribe_events"}, on_event)


async def test_failing_callback_does_not_break_the_client(event_server):
    url, _state = event_server

    async def boom(event):
        raise ValueError("handler bug")

    client = WebSocketClient(url, "secret")
    await client.start(connect_timeout=5)
    await client.subscribe(SUBSCRIBE, boom)
    await asyncio.sleep(0.2)
    assert client.connected
    await client.stop()
