"""
SQLite dataset schema, migrations, and writer connection for agent telemetry.

Usage:
    conn = open_store("/data/telemetry.sqlite")
    # conn is ready: WAL mode, foreign keys ON, schema at SCHEMA_VERSION.
    # Pass it to SqliteSpanExporter (T10).
"""

import sqlite3

SCHEMA_VERSION = 1

# Each entry is a SQL script to apply when upgrading to that schema version.
# Index 0 = migration to version 1, index 1 = migration to version 2, etc.
# Do NOT include CREATE TABLE schema_version here — apply_migrations handles it.
_MIGRATIONS = [
    # Migration 1: full dataset schema
    """
CREATE TABLE requests (
  request_id      TEXT PRIMARY KEY,
  ts_start        TEXT NOT NULL,
  duration_ms     INTEGER,
  channel         TEXT NOT NULL,
  endpoint        TEXT,
  device_id       TEXT,
  thread_id       TEXT,
  input_text      TEXT NOT NULL,
  output_text     TEXT,
  path            TEXT NOT NULL,
  outcome         TEXT NOT NULL,
  error           TEXT,
  model           TEXT,
  provider        TEXT,
  app_version     TEXT,
  max_tier        INTEGER,
  prompt_hash     TEXT REFERENCES prompt_snapshots(hash),
  toolset_hash    TEXT REFERENCES toolset_snapshots(hash),
  steps           INTEGER,
  ttft_ms         INTEGER
);

CREATE TABLE model_calls (
  span_id         TEXT PRIMARY KEY,
  request_id      TEXT NOT NULL REFERENCES requests(request_id),
  step            INTEGER NOT NULL,
  ts_start        TEXT NOT NULL,
  duration_ms     INTEGER,
  model           TEXT,
  fast_path       INTEGER NOT NULL DEFAULT 0,
  tools_offered   TEXT NOT NULL,
  messages_count  INTEGER,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  thinking_text   TEXT,
  content_text    TEXT,
  tool_calls      TEXT,
  finish_reason   TEXT
);

CREATE TABLE tool_calls (
  span_id         TEXT PRIMARY KEY,
  request_id      TEXT NOT NULL REFERENCES requests(request_id),
  seq             INTEGER NOT NULL,
  tool            TEXT NOT NULL,
  call_id         TEXT,
  args_json       TEXT,
  status          TEXT,
  error_code      TEXT,
  duration_ms     INTEGER,
  result_text     TEXT
);

CREATE TABLE fast_path_decisions (
  request_id      TEXT PRIMARY KEY REFERENCES requests(request_id),
  backend         TEXT NOT NULL,
  menu_hash       TEXT REFERENCES menu_snapshots(hash),
  entity_id       TEXT,
  confidence      REAL,
  threshold       REAL,
  accepted        INTEGER,
  skip_reason     TEXT,
  duration_ms     INTEGER
);

CREATE TABLE labels (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id         TEXT NOT NULL REFERENCES requests(request_id),
  ts                 TEXT NOT NULL,
  source             TEXT NOT NULL,
  rating             INTEGER,
  correct_tool       TEXT,
  correct_entity_id  TEXT,
  note               TEXT
);

CREATE TABLE prompt_snapshots  (hash TEXT PRIMARY KEY, text TEXT NOT NULL,         first_seen TEXT NOT NULL);
CREATE TABLE toolset_snapshots (hash TEXT PRIMARY KEY, schemas_json TEXT NOT NULL, first_seen TEXT NOT NULL);
CREATE TABLE menu_snapshots    (hash TEXT PRIMARY KEY, items_json TEXT NOT NULL,   first_seen TEXT NOT NULL);

CREATE INDEX idx_requests_ts ON requests(ts_start);
CREATE INDEX idx_model_calls_req ON model_calls(request_id);
CREATE INDEX idx_tool_calls_req ON tool_calls(request_id);
CREATE INDEX idx_tool_calls_tool ON tool_calls(tool);
CREATE INDEX idx_labels_req ON labels(request_id);
""",
]


def insert_label(
    conn: sqlite3.Connection,
    request_id: str,
    source: str,
    rating: int | None = None,
    correct_tool: str | None = None,
    correct_entity_id: str | None = None,
    note: str | None = None,
) -> int:
    """Insert a row into the labels table and return its rowid.

    ``ts`` is set to UTC now in ISO-8601 format (e.g. ``2026-09-17T12:34:56Z``).
    The caller is responsible for ensuring ``request_id`` exists in the requests
    table; a missing FK will raise an ``IntegrityError`` (FK enforcement is ON).
    """
    import datetime

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "INSERT INTO labels "
        "(request_id, ts, source, rating, correct_tool, correct_entity_id, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (request_id, ts, source, rating, correct_tool, correct_entity_id, note),
    )
    conn.commit()
    return cur.lastrowid


def open_store(path: str, *, check_same_thread: bool = False) -> sqlite3.Connection:
    """Open (or create) the telemetry SQLite database.

    Returns a connection with WAL journal mode, foreign keys enabled,
    and all schema migrations applied up to SCHEMA_VERSION.

    ``check_same_thread=False`` is the default because the telemetry store is
    shared between the lifespan (async) thread and request handler threads in
    the FastAPI app.  Callers that want strict per-thread ownership can pass
    ``check_same_thread=True``.
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    return conn


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema to SCHEMA_VERSION.

    Safe to call on an already-migrated database: only unapplied migrations
    are executed. The schema_version table always ends with exactly one row.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row[0] if row else 0
    for i, sql in enumerate(_MIGRATIONS, start=1):
        if i > current:
            conn.executescript(sql)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
