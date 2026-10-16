"""Retention for the two local databases that otherwise grow forever.

- Telemetry: every request stores its full input and output text. Requests
  older than `telemetry_retention_days` are deleted with their model calls,
  tool calls and fast-path decisions. A request someone labelled is kept: the
  labels are hand-made eval data.
- Conversation threads: every new UI conversation is a fresh thread id, and a
  durable checkpointer keeps all of them. A thread nobody wrote to for
  `thread_retention_days` is deleted with its checkpoints and its native-text
  overlay rows.

Thread activity is read from the checkpoints table itself, so every writer
(chat, events, CLI) counts without having to report in: once a day the job
remembers each thread's newest checkpoint id, and a thread whose id has not
changed since the cutoff is stale. A thread first seen by this job counts as
active from that moment.

A retention of 0 days keeps everything. Nothing here raises into the app.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger("agent.retention")

# First run shortly after boot, then once a day.
_FIRST_RUN_DELAY_S = 60
_INTERVAL_S = 24 * 60 * 60

_ACTIVITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_activity (
    thread_id          TEXT PRIMARY KEY,
    last_checkpoint_id TEXT NOT NULL,
    last_seen          TEXT NOT NULL
)
"""

# Child tables first: they reference requests(request_id).
_TELEMETRY_CHILD_TABLES = ("model_calls", "tool_calls", "fast_path_decisions")


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def prune_telemetry(conn: sqlite3.Connection, days: int, now: datetime) -> int:
    """Delete unlabelled requests older than `days`. Returns how many."""
    if days <= 0:
        return 0
    cutoff = _iso(now - timedelta(days=days))
    old = (
        "SELECT request_id FROM requests WHERE ts_start < ? "
        "AND request_id NOT IN (SELECT request_id FROM labels)"
    )
    for table in _TELEMETRY_CHILD_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE request_id IN ({old})", (cutoff,))
    cur = conn.execute(f"DELETE FROM requests WHERE request_id IN ({old})", (cutoff,))
    conn.commit()
    return cur.rowcount


class ThreadActivity:
    """When each checkpointed thread last changed. Lives in the checkpoint DB."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_ACTIVITY_SCHEMA)
        self._conn.commit()

    def stale_threads(self, days: int, now: datetime) -> list[str]:
        """Record what changed since the last call, then return the threads
        whose newest checkpoint is unchanged for `days`."""
        try:
            rows = self._conn.execute(
                "SELECT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id"
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # no checkpoints table yet: nothing was ever saved
        known = {}
        for thread_id, checkpoint_id in self._conn.execute(
            "SELECT thread_id, last_checkpoint_id FROM thread_activity"
        ):
            known[thread_id] = checkpoint_id

        stamp = _iso(now)
        for thread_id, checkpoint_id in rows:
            if known.get(thread_id) != checkpoint_id:
                self._conn.execute(
                    "INSERT OR REPLACE INTO thread_activity VALUES (?, ?, ?)",
                    (thread_id, checkpoint_id, stamp),
                )
        self._conn.commit()

        cutoff = _iso(now - timedelta(days=days))
        stale = []
        for (thread_id,) in self._conn.execute(
            "SELECT thread_id FROM thread_activity WHERE last_seen < ?", (cutoff,)
        ):
            stale.append(thread_id)
        return stale

    def forget(self, thread_id: str) -> None:
        self._conn.execute("DELETE FROM thread_activity WHERE thread_id = ?", (thread_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


async def prune_threads(checkpointer, activity: ThreadActivity, overlay, days: int,
                        now: datetime) -> int:
    """Delete threads idle for `days`. Returns how many."""
    if days <= 0:
        return 0
    stale = activity.stale_threads(days, now)
    for thread_id in stale:
        await checkpointer.adelete_thread(thread_id)
        if overlay is not None:
            overlay.forget(thread_id)
        activity.forget(thread_id)
    return len(stale)


class RetentionJob:
    """Runs both prunes once a day. Either half may be absent (telemetry off,
    in-memory checkpointer)."""

    def __init__(self, settings, telemetry_conn=None, checkpointer=None,
                 activity: ThreadActivity | None = None, overlay=None) -> None:
        self._settings = settings
        self._telemetry_conn = telemetry_conn
        self._checkpointer = checkpointer
        self._activity = activity
        self._overlay = overlay
        self._task: asyncio.Task | None = None

    async def run_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self._telemetry_conn is not None:
            try:
                removed = prune_telemetry(
                    self._telemetry_conn, self._settings.telemetry_retention_days, now)
                if removed:
                    log.info("retention: removed %d old telemetry requests", removed)
            except Exception:
                log.warning("retention: telemetry prune failed", exc_info=True)
        if self._checkpointer is not None and self._activity is not None:
            try:
                removed = await prune_threads(
                    self._checkpointer, self._activity, self._overlay,
                    self._settings.thread_retention_days, now)
                if removed:
                    log.info("retention: removed %d idle conversation threads", removed)
            except Exception:
                log.warning("retention: thread prune failed", exc_info=True)

    async def _loop(self) -> None:
        await asyncio.sleep(_FIRST_RUN_DELAY_S)
        while True:
            await self.run_once()
            await asyncio.sleep(_INTERVAL_S)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
