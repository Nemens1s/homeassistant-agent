"""Persistent action audit: append-only SQLite rows for tier-2 tool calls.
Stdout logging remains the live view; this survives addon restarts so
"what did the agent do last Tuesday" has an answer. A failing audit write
is logged and swallowed — the action already happened and the envelope
must still reach the model."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("agent.audit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    entity_id TEXT,
    domain TEXT,
    service TEXT,
    params_json TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    duration_ms INTEGER
)
"""


class AuditSink:
    def __init__(self, db_path: str):
        self._conn: sqlite3.Connection | None = None
        if not db_path:
            return
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    async def record(self, *, thread_id: str, tool: str, entity_id: str,
                     domain: str, service: str, params_json: str,
                     status: str, error_code: str | None, duration_ms: int) -> None:
        if self._conn is None:
            return
        row = (datetime.now(timezone.utc).isoformat(timespec="seconds"),
               thread_id, tool, entity_id, domain, service, params_json,
               status, error_code, duration_ms)
        try:
            await asyncio.to_thread(self._write, row)
        except Exception:
            log.exception("audit write failed (tool=%s)", tool)

    def _write(self, row: tuple) -> None:
        # Single-writer assumed: addon is single-user, tool calls are sequential per turn.
        self._conn.execute(
            "INSERT INTO actions (ts, thread_id, tool, entity_id, domain, service,"
            " params_json, status, error_code, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
