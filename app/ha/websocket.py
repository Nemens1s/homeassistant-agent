"""Persistent Home Assistant websocket client.

One connection for the process lifetime: auth handshake, id-correlated
request/response via futures, reconnect with backoff. Used for the
websocket-only APIs (area/device/entity registries). Event subscriptions
(subscribe_trigger only) are re-sent on every reconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

log = logging.getLogger("agent.ws")

# Message types this client may send. The WS API can mutate HA
# (call_service etc.); restricting types makes the client's read-only
# property structural, like the REST client's GET-only surface.
READ_ONLY_COMMANDS: tuple[str, ...] = (
    "config/area_registry/list",
    "config/device_registry/list",
    "config/entity_registry/list",
    "config/label_registry/list",
    "ping",
)

# Commands allowed only for UI management (not agent tools).
MANAGEMENT_COMMANDS: tuple[str, ...] = (
    "call_service",
    "config/entity_registry/update",
)

# Subscription commands. Only subscribe_trigger: HA evaluates the trigger
# server-side, so the client receives exactly the state changes it asked for
# instead of the whole event bus.
SUBSCRIPTION_COMMANDS: tuple[str, ...] = ("subscribe_trigger",)

EventCallback = Callable[[dict], Awaitable[None]]


def ws_is_ready(ws: Any) -> bool:
    """True when *ws* can serve a request right now. A client that is still
    (re)connecting counts as not ready, so callers degrade at once instead of
    waiting out the request timeout. Test fakes without a `connected`
    attribute count as ready."""
    if ws is None:
        return False
    return bool(getattr(ws, "connected", True))


class WebSocketClient:
    def __init__(self, url: str, token: str, request_timeout: float = 10.0):
        self._url = url
        self._token = token
        self._timeout = request_timeout
        self._conn: Any = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._cache: dict[str, tuple[float, Any]] = {}
        self._runner: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._closing = False
        # What we want to be subscribed to survives reconnects; _active maps
        # the current connection's message ids to callbacks.
        self._subscriptions: list[tuple[dict, EventCallback]] = []
        self._active: dict[int, EventCallback] = {}
        self._callback_tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self, connect_timeout: float = 10.0) -> None:
        self._closing = False
        self._runner = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=connect_timeout)
        except TimeoutError:
            await self.stop()
            raise

    def start_background(self) -> None:
        """Keep (re)connecting in the background without waiting for the first
        connection — used when HA is not up yet at startup."""
        if self._runner is not None and not self._runner.done():
            return
        self._closing = False
        self._runner = asyncio.create_task(self._run())

    async def subscribe(self, message: dict, callback: EventCallback) -> None:
        """Subscribe now if connected, and again after every reconnect.

        Raises:
            PermissionError: the message type is not in SUBSCRIPTION_COMMANDS.
        """
        if message.get("type") not in SUBSCRIPTION_COMMANDS:
            raise PermissionError(
                f"websocket subscription not allowed: {message.get('type')!r}"
            )
        self._subscriptions.append((message, callback))
        conn = self._conn
        if conn is None or not self.connected:
            return  # _run sends it after the next auth_ok
        try:
            await self._send_subscription(conn, message, callback)
        except Exception as exc:
            log.warning("websocket subscribe failed (%s) — retrying on reconnect", exc)

    async def _send_subscription(self, conn: Any, message: dict, callback: EventCallback) -> None:
        msg_id = self._next_id
        self._next_id += 1
        self._active[msg_id] = callback
        await conn.send(json.dumps({**message, "id": msg_id}))

    async def _resubscribe(self, conn: Any) -> None:
        # Index loop over the live list, not a snapshot: a subscribe() that
        # lands while we are sending is picked up here instead of being lost.
        index = 0
        while index < len(self._subscriptions):
            message, callback = self._subscriptions[index]
            await self._send_subscription(conn, message, callback)
            index += 1

    async def _run_callback(self, callback: EventCallback, event: dict) -> None:
        try:
            await callback(event)
        except Exception:
            log.exception("websocket event callback failed")

    async def stop(self) -> None:
        self._closing = True
        runner = self._runner
        self._runner = None
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                if not runner.cancelled():  # cancellation was ours, not the runner's
                    raise
            except Exception:
                pass
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self._connected.clear()
        for task in list(self._callback_tasks):
            task.cancel()
        self._fail_pending(ConnectionError("websocket client stopped"))

    async def _send_request(self, msg_type: str, **payload: Any) -> Any:
        deadline = time.monotonic() + self._timeout
        await asyncio.wait_for(
            self._connected.wait(), timeout=max(0.0, deadline - time.monotonic())
        )
        conn = self._conn
        if conn is None:
            raise ConnectionError("websocket disconnected")
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await conn.send(json.dumps({"id": msg_id, "type": msg_type, **payload}))
            return await asyncio.wait_for(
                fut, timeout=max(0.0, deadline - time.monotonic())
            )
        except Exception as exc:
            if isinstance(exc, (TimeoutError, RuntimeError)):
                raise
            raise ConnectionError(f"websocket send failed: {exc}") from exc
        finally:
            self._pending.pop(msg_id, None)

    async def request(self, msg_type: str, **payload: Any) -> Any:
        """Send one read-only command and await its correlated result.

        Raises:
            PermissionError: msg_type is not in READ_ONLY_COMMANDS.
            TimeoutError: no connection or no response within request_timeout
                (one total budget across both waits).
            ConnectionError: connection dropped before/while sending.
            RuntimeError: HA answered with success=False.
        """
        if msg_type not in READ_ONLY_COMMANDS:
            raise PermissionError(f"websocket command not allowed: {msg_type!r}")
        return await self._send_request(msg_type, **payload)

    async def management_request(self, msg_type: str, **payload: Any) -> Any:
        """Send a management command (UI-only, not available to agent tools).

        Raises:
            PermissionError: msg_type is not in MANAGEMENT_COMMANDS.
        """
        if msg_type not in MANAGEMENT_COMMANDS:
            raise PermissionError(f"websocket management command not allowed: {msg_type!r}")
        return await self._send_request(msg_type, **payload)

    async def request_cached(self, msg_type: str, ttl: float = 60.0) -> Any:
        hit = self._cache.get(msg_type)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]
        result = await self.request(msg_type)
        self._cache[msg_type] = (time.monotonic(), result)
        return result

    async def _run(self) -> None:
        backoff = 1
        while not self._closing:
            try:
                async with websockets.connect(self._url) as conn:
                    await self._auth(conn)
                    self._conn = conn
                    await self._resubscribe(conn)
                    self._connected.set()
                    backoff = 1
                    log.info("websocket connected: %s", self._url)
                    async for raw in conn:
                        self._dispatch(json.loads(raw))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("websocket dropped: %s — reconnecting in %ss", exc, backoff)
            self._handle_disconnect(ConnectionError("websocket disconnected"))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _handle_disconnect(self, exc: Exception) -> None:
        self._connected.clear()
        self._conn = None
        self._cache.clear()
        self._active.clear()  # subscription ids belong to one connection
        self._fail_pending(exc)

    async def _auth(self, conn: Any) -> None:
        msg = json.loads(await conn.recv())
        if msg.get("type") != "auth_required":
            raise ConnectionError(f"unexpected first message: {msg.get('type')}")
        await conn.send(json.dumps({"type": "auth", "access_token": self._token}))
        msg = json.loads(await conn.recv())
        if msg.get("type") != "auth_ok":
            raise ConnectionError(f"websocket auth failed: {msg.get('message', '')}")

    def _dispatch(self, msg: dict) -> None:
        msg_id = msg.get("id", -1)
        if msg.get("type") == "event":
            callback = self._active.get(msg_id)
            if callback is not None:
                # A task, so a slow handler never blocks the reader loop.
                task = asyncio.create_task(self._run_callback(callback, msg.get("event") or {}))
                self._callback_tasks.add(task)
                task.add_done_callback(self._callback_tasks.discard)
            return
        if msg_id in self._active:
            # The ack of a subscription; only a rejection needs handling.
            if not msg.get("success"):
                error = msg.get("error") or {}
                log.warning("websocket subscription rejected: %s", error.get("message", ""))
                self._active.pop(msg_id, None)
            return
        fut = self._pending.pop(msg_id, None)
        if fut is None or fut.done():
            return
        if msg.get("success"):
            fut.set_result(msg.get("result"))
        else:
            error = msg.get("error") or {}
            fut.set_exception(RuntimeError(error.get("message", "websocket command failed")))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
