"""Native-text overlay for the conversation history.

The checkpointer stores English messages — that is the point of the design, and
the agent's own memory stays English. But a reloaded chat should show what the
user actually typed and what they were actually told, so every translated turn
is also written here and substituted back into GET /api/history.

The table lives in the checkpoint database, next to the messages it annotates:
one file to back up, one file to wipe. Nothing here raises into a request — a
missing or unusable database just means the history stays English.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

log = logging.getLogger("agent.i18n")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lang_overlay (
    thread_id     TEXT NOT NULL,
    turn_index    INTEGER NOT NULL,
    language      TEXT NOT NULL,
    original_text TEXT NOT NULL,
    english_text  TEXT NOT NULL,
    reply_en      TEXT,
    reply_native  TEXT,
    PRIMARY KEY (thread_id, turn_index)
)
"""

_COLUMNS = ("turn_index", "language", "original_text", "english_text", "reply_en", "reply_native")


class LangOverlay:
    def __init__(self, path: str, max_rows_per_thread: int = 200) -> None:
        self._max_rows = max_rows_per_thread
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            conn.commit()
            self._conn = conn
        except Exception:
            log.warning("lang: overlay unavailable at %s — history stays English", path, exc_info=True)

    def record(
        self,
        thread_id: str,
        language: str,
        original_text: str,
        english_text: str,
        reply_en: str | None,
        reply_native: str | None,
    ) -> None:
        """Remember one translated turn. Never raises."""
        if self._conn is None:
            return
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT COALESCE(MAX(turn_index) + 1, 0) FROM lang_overlay WHERE thread_id = ?",
                    (thread_id,),
                )
                turn_index = cur.fetchone()[0]
                self._conn.execute(
                    "INSERT OR REPLACE INTO lang_overlay "
                    "(thread_id, turn_index, language, original_text, english_text, reply_en, reply_native) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (thread_id, turn_index, language, original_text, english_text,
                     reply_en, reply_native),
                )
                self._conn.execute(
                    "DELETE FROM lang_overlay WHERE thread_id = ? AND turn_index <= ?",
                    (thread_id, turn_index - self._max_rows),
                )
                self._conn.commit()
        except Exception:
            log.warning("lang: could not record overlay row", exc_info=True)

    def rows_for(self, thread_id: str) -> list[dict]:
        """Rows for a thread, oldest first. Never raises."""
        if self._conn is None:
            return []
        try:
            with self._lock:
                cur = self._conn.execute(
                    f"SELECT {', '.join(_COLUMNS)} FROM lang_overlay "
                    "WHERE thread_id = ? ORDER BY turn_index",
                    (thread_id,),
                )
                fetched = cur.fetchall()
        except Exception:
            log.warning("lang: could not read overlay rows", exc_info=True)
            return []

        rows = []
        for record in fetched:
            row = {}
            for name, value in zip(_COLUMNS, record):
                row[name] = value
            rows.append(row)
        return rows

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def apply_overlay(turns: list[dict], rows: list[dict]) -> list[dict]:
    """Substitute native texts into a display transcript.

    Turns are matched by their English text rather than by position: the
    history cap prunes old messages, and turns from before the language layer
    was switched on have no row at all. Rows are consumed in order, so two
    identical questions get their own replies back.
    """
    remaining = list(rows)
    result = []
    pending = None

    for turn in turns:
        if turn["role"] == "user":
            pending = None
            index = None
            for i, row in enumerate(remaining):
                if row["english_text"] == turn["text"]:
                    index = i
                    break
            if index is not None:
                pending = remaining[index]
                # Rows before the match belong to turns the history cap dropped.
                remaining = remaining[index + 1:]
                result.append({"role": "user", "text": pending["original_text"]})
                continue
        elif pending is not None:
            if pending["reply_native"] and turn["text"] == pending["reply_en"]:
                result.append({"role": "bot", "text": pending["reply_native"]})
                pending = None
                continue
            pending = None

        result.append(turn)

    return result
