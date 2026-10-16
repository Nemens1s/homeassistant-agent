"""Liveness of one HTTP dependency (the LLM server, the fast-path service).

State is "unknown" until the first probe, then "up" or "down". A background
loop probes every down_interval_s while not up (so recovery is noticed fast)
and every up_interval_s while up. Callers also report failures they hit
themselves with mark_down() — the passive signal — so requests stop waiting on
a dead service before the next probe runs. Nothing here raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

log = logging.getLogger("agent.health")

UNKNOWN = "unknown"
UP = "up"
DOWN = "down"

Probe = Callable[[], Awaitable[bool]]
RecoveryCallback = Callable[[], Awaitable[None]]


class ServiceHealth:
    def __init__(
        self,
        name: str,
        probe: Probe,
        down_interval_s: float = 15.0,
        up_interval_s: float = 60.0,
        initial: str = UNKNOWN,
    ) -> None:
        self.name = name
        self._probe = probe
        self._down_interval_s = down_interval_s
        self._up_interval_s = up_interval_s
        self.state = initial
        self.since = time.time()
        self.last_error: str | None = None
        self._callbacks: list[RecoveryCallback] = []
        self._task: asyncio.Task | None = None

    @property
    def is_down(self) -> bool:
        return self.state == DOWN

    def on_recovery(self, callback: RecoveryCallback) -> None:
        self._callbacks.append(callback)

    def mark_down(self, error: object = None) -> None:
        if error is not None:
            self.last_error = str(error) or type(error).__name__
        if self.state == DOWN:
            return
        self.state = DOWN
        self.since = time.time()
        log.warning("%s: down (%s)", self.name, self.last_error)

    async def mark_up(self) -> None:
        previous = self.state
        if previous == UP:
            return
        downtime_s = round(time.time() - self.since)
        self.state = UP
        self.since = time.time()
        self.last_error = None
        if previous != DOWN:
            return  # unknown -> up is the first probe, not a recovery
        log.info("%s: up again after %ss", self.name, downtime_s)
        for callback in self._callbacks:
            try:
                await callback()
            except Exception:
                log.exception("%s: recovery callback failed", self.name)

    async def check(self) -> None:
        error: object = None
        try:
            ok = await self._probe()
            if not ok:
                error = "probe failed"
        except Exception as exc:
            ok = False
            error = exc
        if ok:
            await self.mark_up()
        else:
            self.mark_down(error)

    def interval(self) -> float:
        if self.state == UP:
            return self._up_interval_s
        return self._down_interval_s

    async def _loop(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(self.interval())

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def since_hhmm(self) -> str:
        return datetime.fromtimestamp(self.since).strftime("%H:%M")

    def snapshot(self) -> dict:
        since = datetime.fromtimestamp(self.since).astimezone()
        return {
            "state": self.state,
            "since": since.isoformat(timespec="seconds"),
            "error": self.last_error,
        }
