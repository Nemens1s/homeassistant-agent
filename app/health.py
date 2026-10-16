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
        # Set by a passive mark_down so the loop re-probes now instead of
        # sleeping out a long up-interval. Created in start(): constructing a
        # tracker must not need a running event loop.
        self._wake: asyncio.Event | None = None

    @property
    def is_down(self) -> bool:
        return self.state == DOWN

    def on_recovery(self, callback: RecoveryCallback) -> None:
        self._callbacks.append(callback)

    def set_probe(self, probe: Probe) -> None:
        self._probe = probe

    def mark_down(self, error: object = None) -> None:
        """Passive signal from a caller that hit the failure itself. A fresh
        transition wakes the loop so the next probe runs right away."""
        if self._set_down(error) and self._wake is not None:
            self._wake.set()

    def _set_down(self, error: object) -> bool:
        """Returns True when the state changed to down."""
        if error is not None:
            self.last_error = str(error) or type(error).__name__
        if self.state == DOWN:
            return False
        self.state = DOWN
        self.since = time.time()
        log.warning("%s: down (%s)", self.name, self.last_error)
        return True

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
            # Not mark_down: the probe's own failure must not wake the loop
            # (that would re-probe in a tight loop).
            self._set_down(error)

    def interval(self) -> float:
        if self.state == UP:
            return self._up_interval_s
        return self._down_interval_s

    async def _loop(self) -> None:
        while True:
            await self.check()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval())
            except TimeoutError:
                pass  # a normal tick
            self._wake.clear()

    def start(self) -> None:
        if self._wake is None:
            self._wake = asyncio.Event()
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
