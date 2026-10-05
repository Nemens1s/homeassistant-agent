"""Memory notes: one-shot instructions the agent saves now and the harness
fires later — when a watched entity changes state, or when the HA clock
reaches a time.

The table lives in the checkpoint database, next to the conversations that
created the notes: one file to back up. Like the language overlay
(app/i18n/store.py) it is plain sqlite3 behind a lock — every query is one
indexed lookup — and nothing here raises: a broken database means notes are
unavailable, never a failed request.

Times: created_at / expires_at / fired_at are ISO-8601 UTC. Time notes keep
fire_at_local / expires_at_local in the HA clock's wall time as
"YYYY-MM-DD HH:MM" (app/events/clock.py), so string order is time order.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from app.events.clock import to_key

log = logging.getLogger("agent.memory")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_notes (
    id                   INTEGER PRIMARY KEY,
    trigger_kind         TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    entity_id            TEXT,
    to_state             TEXT,
    expires_at           TEXT,
    fire_at_local        TEXT,
    expires_at_local     TEXT,
    instruction          TEXT NOT NULL,
    instruction_original TEXT,
    language             TEXT NOT NULL DEFAULT 'en',
    tags                 TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL DEFAULT 'pending',
    fired_at             TEXT,
    source_thread_id     TEXT
);
CREATE INDEX IF NOT EXISTS memory_notes_state ON memory_notes(status, entity_id);
CREATE INDEX IF NOT EXISTS memory_notes_time ON memory_notes(status, fire_at_local);
"""

# Same order as the Note fields below.
_COLUMNS = (
    "id", "trigger_kind", "kind", "created_at", "entity_id", "to_state",
    "expires_at", "fire_at_local", "expires_at_local", "instruction",
    "instruction_original", "language", "tags", "status", "fired_at",
    "source_thread_id",
)
_TAGS_INDEX = _COLUMNS.index("tags")


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Note:
    id: int
    trigger_kind: str  # "state" | "time"
    kind: str  # "action" | "reminder"
    created_at: str
    entity_id: str | None
    to_state: str | None
    expires_at: str | None
    fire_at_local: str | None
    expires_at_local: str | None
    instruction: str  # always English
    instruction_original: str | None  # what the user said, when it differed
    language: str  # the user's language, for notifications
    tags: list[str]
    status: str  # pending | fired | expired | cancelled
    fired_at: str | None
    source_thread_id: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _row_to_note(row: tuple) -> Note:
    values = list(row)
    try:
        values[_TAGS_INDEX] = json.loads(values[_TAGS_INDEX] or "[]")
    except ValueError:
        values[_TAGS_INDEX] = []
    return Note(*values)


class NoteStore:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        target = path or ":memory:"
        try:
            conn = sqlite3.connect(target, check_same_thread=False)
            if path:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        except Exception:
            log.warning("memory: note store unavailable at %s — notes disabled", target, exc_info=True)

    @property
    def available(self) -> bool:
        return self._conn is not None

    # ---------- writes ----------
    def add_state_note(self, *, entity_id: str, to_state: str | None, instruction: str,
                       instruction_original: str | None, kind: str, language: str,
                       tags: list[str], expires_at: datetime, now: datetime,
                       source_thread_id: str | None) -> int | None:
        return self._insert({
            "trigger_kind": "state",
            "kind": kind,
            "created_at": _utc(now),
            "entity_id": entity_id,
            "to_state": to_state,
            "expires_at": _utc(expires_at),
            "instruction": instruction,
            "instruction_original": instruction_original,
            "language": language,
            "tags": json.dumps(list(tags)),
            "source_thread_id": source_thread_id,
        })

    def add_time_note(self, *, fire_at_local: datetime, expires_at_local: datetime,
                      instruction: str, instruction_original: str | None, kind: str,
                      language: str, tags: list[str], now: datetime,
                      source_thread_id: str | None) -> int | None:
        return self._insert({
            "trigger_kind": "time",
            "kind": kind,
            "created_at": _utc(now),
            "fire_at_local": to_key(fire_at_local),
            "expires_at_local": to_key(expires_at_local),
            "instruction": instruction,
            "instruction_original": instruction_original,
            "language": language,
            "tags": json.dumps(list(tags)),
            "source_thread_id": source_thread_id,
        })

    def cancel(self, note_id: int) -> bool:
        changed = self._execute(
            "UPDATE memory_notes SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (note_id,),
        )
        return changed == 1

    def mark_fired(self, note_ids: list[int], now: datetime) -> None:
        for note_id in note_ids:
            self._execute(
                "UPDATE memory_notes SET status = 'fired', fired_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (_utc(now), note_id),
            )

    # ---------- reads ----------
    def list_pending(self, now: datetime) -> list[Note]:
        self._expire_state_notes(now)
        return self._select("status = 'pending' ORDER BY id", ())

    def list_recent(self, limit: int = 100) -> list[Note]:
        return self._select("1 = 1 ORDER BY id DESC LIMIT ?", (limit,))

    def match_state(self, entity_id: str, new_state: str, now: datetime) -> list[Note]:
        self._expire_state_notes(now)
        return self._select(
            "status = 'pending' AND trigger_kind = 'state' AND entity_id = ? "
            "AND (to_state IS NULL OR LOWER(to_state) = LOWER(?)) ORDER BY id",
            (entity_id, new_state),
        )

    def due_time(self, now_local: datetime) -> list[Note]:
        key = to_key(now_local)
        self._execute(
            "UPDATE memory_notes SET status = 'expired' WHERE status = 'pending' "
            "AND trigger_kind = 'time' AND expires_at_local <= ?",
            (key,),
        )
        # "<=" not "=": a tick missed during a reconnect fires on the next one.
        return self._select(
            "status = 'pending' AND trigger_kind = 'time' AND fire_at_local <= ? "
            "ORDER BY fire_at_local, id",
            (key,),
        )

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ---------- internals ----------
    def _expire_state_notes(self, now: datetime) -> None:
        self._execute(
            "UPDATE memory_notes SET status = 'expired' WHERE status = 'pending' "
            "AND trigger_kind = 'state' AND expires_at <= ?",
            (_utc(now),),
        )

    def _insert(self, values: dict) -> int | None:
        if self._conn is None:
            return None
        names = list(values)
        placeholders = ", ".join(["?"] * len(names))
        sql = f"INSERT INTO memory_notes ({', '.join(names)}) VALUES ({placeholders})"
        try:
            with self._lock:
                cur = self._conn.execute(sql, list(values.values()))
                self._conn.commit()
                return cur.lastrowid
        except Exception:
            log.warning("memory: could not save note", exc_info=True)
            return None

    def _execute(self, sql: str, params: tuple) -> int:
        if self._conn is None:
            return 0
        try:
            with self._lock:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
                return cur.rowcount
        except Exception:
            log.warning("memory: note update failed", exc_info=True)
            return 0

    def _select(self, where: str, params: tuple) -> list[Note]:
        if self._conn is None:
            return []
        sql = f"SELECT {', '.join(_COLUMNS)} FROM memory_notes WHERE {where}"
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except Exception:
            log.warning("memory: could not read notes", exc_info=True)
            return []
        notes = []
        for row in rows:
            notes.append(_row_to_note(row))
        return notes
