"""
SQLite dataset schema, migrations, and writer connection for agent telemetry.

Usage:
    conn = open_store("/data/telemetry.sqlite")
    # conn is ready: WAL mode, foreign keys ON, schema at SCHEMA_VERSION.
    # Pass it to SqliteSpanExporter (T10).
"""

import sqlite3

SCHEMA_VERSION = 2

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
    # Migration 2: derived views for the metrics UI and export CLI.
    #
    # fast_path_dataset — one row per classified request (those with a row in
    # fast_path_decisions).  Columns:
    #   request_id, ts_start, utterance (= input_text), backend, menu_hash,
    #   prediction (= entity_id from fp decision), confidence, threshold,
    #   accepted, latest_label_rating (rating from the most-recent label row,
    #   NULL if none), agent_reference (entity_id from the first
    #   trigger_action tool call when path=agent, NULL for fast-path hits
    #   or when none was called).
    #
    # tool_offer_stats — per toolset_hash, per tool: how often offered and
    # chosen, chosen/offered ratio, error rate, repeated_call rate.
    # Fast-path model_calls are excluded (fast_path = 1).
    # "offered" = count of model_calls rows in which the JSON tools_offered
    #   array contains the tool name.
    # "chosen" = count of tool_calls rows for that tool in the same toolset.
    # Columns: toolset_hash, tool, times_offered, times_chosen, chosen_ratio,
    #          error_rate, repeated_call_rate.
    #
    # request_quality — per request: steps, total_thinking_chars,
    # thinking_before_first_tool (chars of thinking_text in model_calls rows
    # with step < the step of the first tool call, or all thinking if no tool
    # calls), repeated_calls (count of tool_calls with error_code =
    # 'repeated_call'), distinct_tools (count of distinct tool names used).
    """
CREATE VIEW IF NOT EXISTS fast_path_dataset AS
SELECT
    r.request_id,
    r.ts_start,
    r.input_text                                    AS utterance,
    fp.backend,
    fp.menu_hash,
    fp.entity_id                                    AS prediction,
    fp.confidence,
    fp.threshold,
    fp.accepted,
    (
        SELECT l.rating
        FROM labels l
        WHERE l.request_id = r.request_id
        ORDER BY l.ts DESC
        LIMIT 1
    )                                               AS latest_label_rating,
    CASE WHEN fp.accepted = 0 THEN (
        SELECT tc.args_json
        FROM tool_calls tc
        WHERE tc.request_id = r.request_id
          AND tc.tool = 'trigger_action'
          AND (tc.call_id IS NULL OR tc.call_id NOT LIKE 'fastpath-%')
        ORDER BY tc.seq
        LIMIT 1
    ) ELSE NULL END                                 AS agent_reference
FROM requests r
JOIN fast_path_decisions fp ON fp.request_id = r.request_id;

CREATE VIEW IF NOT EXISTS tool_offer_stats AS
WITH offered_counts AS (
    -- Count how many non-fast-path model_calls offered each tool.
    -- We join requests to get toolset_hash, then expand tools_offered JSON.
    SELECT
        r.toolset_hash,
        tool_name.value                             AS tool,
        COUNT(*)                                    AS times_offered
    FROM model_calls mc
    JOIN requests r ON r.request_id = mc.request_id
    JOIN json_each(mc.tools_offered) AS tool_name
    WHERE mc.fast_path = 0
      AND r.toolset_hash IS NOT NULL
    GROUP BY r.toolset_hash, tool_name.value
),
chosen_counts AS (
    SELECT
        r.toolset_hash,
        tc.tool,
        COUNT(*)                                    AS times_chosen,
        SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END)
                                                    AS times_error,
        SUM(CASE WHEN tc.error_code = 'repeated_call' THEN 1 ELSE 0 END)
                                                    AS times_repeated_call
    FROM tool_calls tc
    JOIN requests r ON r.request_id = tc.request_id
    WHERE r.toolset_hash IS NOT NULL
    GROUP BY r.toolset_hash, tc.tool
)
SELECT
    oc.toolset_hash,
    oc.tool,
    oc.times_offered,
    COALESCE(cc.times_chosen, 0)                    AS times_chosen,
    CASE WHEN oc.times_offered > 0
         THEN CAST(COALESCE(cc.times_chosen, 0) AS REAL) / oc.times_offered
         ELSE NULL
    END                                             AS chosen_ratio,
    CASE WHEN COALESCE(cc.times_chosen, 0) > 0
         THEN CAST(COALESCE(cc.times_error, 0) AS REAL) / cc.times_chosen
         ELSE NULL
    END                                             AS error_rate,
    CASE WHEN COALESCE(cc.times_chosen, 0) > 0
         THEN CAST(COALESCE(cc.times_repeated_call, 0) AS REAL) / cc.times_chosen
         ELSE NULL
    END                                             AS repeated_call_rate
FROM offered_counts oc
LEFT JOIN chosen_counts cc
    ON cc.toolset_hash = oc.toolset_hash AND cc.tool = oc.tool;

CREATE VIEW IF NOT EXISTS request_quality AS
WITH first_tool_step AS (
    -- Minimum step number of any tool call per request, derived from model_calls
    -- that have a non-empty tool_calls JSON array.
    SELECT
        mc.request_id,
        MIN(mc.step)                                AS first_tool_step
    FROM model_calls mc
    WHERE mc.tool_calls IS NOT NULL
      AND mc.tool_calls != '[]'
    GROUP BY mc.request_id
),
thinking_stats AS (
    SELECT
        mc.request_id,
        SUM(COALESCE(LENGTH(mc.thinking_text), 0))  AS total_thinking_chars,
        SUM(
            CASE
                WHEN ft.first_tool_step IS NULL
                  OR mc.step < ft.first_tool_step
                THEN COALESCE(LENGTH(mc.thinking_text), 0)
                ELSE 0
            END
        )                                           AS thinking_before_first_tool
    FROM model_calls mc
    LEFT JOIN first_tool_step ft ON ft.request_id = mc.request_id
    GROUP BY mc.request_id
),
tool_stats AS (
    SELECT
        tc.request_id,
        SUM(CASE WHEN tc.error_code = 'repeated_call' THEN 1 ELSE 0 END)
                                                    AS repeated_calls,
        COUNT(DISTINCT tc.tool)                     AS distinct_tools
    FROM tool_calls tc
    GROUP BY tc.request_id
)
SELECT
    r.request_id,
    COALESCE(r.steps, 0)                            AS steps,
    COALESCE(ts.total_thinking_chars, 0)            AS total_thinking_chars,
    COALESCE(ts.thinking_before_first_tool, 0)      AS thinking_before_first_tool,
    COALESCE(tl.repeated_calls, 0)                  AS repeated_calls,
    COALESCE(tl.distinct_tools, 0)                  AS distinct_tools
FROM requests r
LEFT JOIN thinking_stats ts ON ts.request_id = r.request_id
LEFT JOIN tool_stats tl ON tl.request_id = r.request_id;
""",
]


def _derived_path(alias: str) -> str:
    """SQL expression for a request's path, derived from model_calls.

    requests.path is written from the fast_path_seen contextvar, which is set
    inside the LangGraph execution and cannot propagate back to the request
    handler, so the stored value is unreliable (always 'agent'). model_calls
    .fast_path, by contrast, is set directly from the model response and is
    authoritative: a request is a fast-path hit iff any of its model steps was.
    """
    return (
        "CASE WHEN EXISTS ("
        f"SELECT 1 FROM model_calls mc "
        f"WHERE mc.request_id = {alias}.request_id AND mc.fast_path = 1"
        ") THEN 'fast_path' ELSE 'agent' END"
    )


def summary(conn: sqlite3.Connection, days: int) -> dict:
    """Return an aggregated summary dict for the last *days* days.

    Shape::

        {
            "requests_by_path": {"fast_path": int, "agent": int, ...},
            "outcome_counts":   {"ok": int, "error": int, ...},
            "fast_path_hit_rate": float | None,   # None when 0 requests
            "duration_p50_by_path": {"fast_path": float | None, ...},
            "duration_p95_by_path": {"fast_path": float | None, ...},
            "ttft_p50_by_path":    {"fast_path": float | None, ...},
            "ttft_p95_by_path":    {"fast_path": float | None, ...},
            "label_counts": {"total": int, "thumbs_up": int, "thumbs_down": int},
        }

    The *days* window is applied via ``ts_start >= datetime('now','-N days')``.
    """
    # SQLite does not have native percentile functions; we compute them via
    # ordering and NTILE / row_number tricks using window functions (available
    # since SQLite 3.25).

    cutoff = f"-{days} days"

    # ---- requests_by_path and outcome_counts --------------------------------
    rows = conn.execute(
        f"SELECT {_derived_path('requests')} AS path, outcome, COUNT(*) "
        "FROM requests "
        "WHERE datetime(ts_start) >= datetime('now', ?) "
        "GROUP BY 1, outcome",
        (cutoff,),
    ).fetchall()

    requests_by_path: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    for path, outcome, cnt in rows:
        requests_by_path[path] = requests_by_path.get(path, 0) + cnt
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + cnt

    total = sum(requests_by_path.values())
    fp_count = requests_by_path.get("fast_path", 0)
    fast_path_hit_rate: float | None = (fp_count / total) if total else None

    # ---- percentiles by path ------------------------------------------------
    # For p50/p95 we fetch all values per path and compute in Python
    # (avoids SQLite version concerns with window functions in NTILE queries).
    def _percentiles(values: list[float | None], p: int) -> float | None:
        clean = sorted(v for v in values if v is not None)
        if not clean:
            return None
        idx = int(len(clean) * p / 100)
        idx = min(idx, len(clean) - 1)
        return clean[idx]

    duration_rows = conn.execute(
        f"SELECT {_derived_path('requests')} AS path, duration_ms "
        "FROM requests "
        "WHERE datetime(ts_start) >= datetime('now', ?)",
        (cutoff,),
    ).fetchall()

    ttft_rows = conn.execute(
        f"SELECT {_derived_path('requests')} AS path, ttft_ms "
        "FROM requests "
        "WHERE datetime(ts_start) >= datetime('now', ?)",
        (cutoff,),
    ).fetchall()

    paths = set(requests_by_path.keys())

    def _group_by_path(rows_list):
        grouped: dict[str, list] = {p: [] for p in paths}
        for path, val in rows_list:
            grouped.setdefault(path, []).append(val)
        return grouped

    dur_by_path = _group_by_path(duration_rows)
    ttft_by_path = _group_by_path(ttft_rows)

    duration_p50_by_path = {p: _percentiles(dur_by_path[p], 50) for p in paths}
    duration_p95_by_path = {p: _percentiles(dur_by_path[p], 95) for p in paths}
    ttft_p50_by_path = {p: _percentiles(ttft_by_path[p], 50) for p in paths}
    ttft_p95_by_path = {p: _percentiles(ttft_by_path[p], 95) for p in paths}

    # ---- label counts -------------------------------------------------------
    lc = conn.execute(
        "SELECT "
        "  COUNT(*) AS total, "
        "  SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS thumbs_up, "
        "  SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS thumbs_down "
        "FROM labels l "
        "JOIN requests r ON r.request_id = l.request_id "
        "WHERE datetime(r.ts_start) >= datetime('now', ?)",
        (cutoff,),
    ).fetchone()

    label_counts = {
        "total": lc[0] or 0,
        "thumbs_up": lc[1] or 0,
        "thumbs_down": lc[2] or 0,
    }

    return {
        "requests_by_path": requests_by_path,
        "outcome_counts": outcome_counts,
        "fast_path_hit_rate": fast_path_hit_rate,
        "duration_p50_by_path": duration_p50_by_path,
        "duration_p95_by_path": duration_p95_by_path,
        "ttft_p50_by_path": ttft_p50_by_path,
        "ttft_p95_by_path": ttft_p95_by_path,
        "label_counts": label_counts,
    }


def recent_requests(
    conn: sqlite3.Connection,
    limit: int,
    cursor: str | None = None,
) -> dict:
    """Return a paginated list of recent requests with drill-down detail.

    Results are sorted newest-first by ``ts_start``.  Pagination uses an
    opaque cursor that encodes the ``ts_start`` of the last returned row.

    Shape::

        {
            "rows": [
                {
                    "request_id": str,
                    "ts_start": str,
                    "channel": str,
                    "path": str,
                    "outcome": str,
                    "duration_ms": int | None,
                    "ttft_ms": int | None,
                    "input_text": str,
                    "output_text": str | None,
                    "model_calls": [...],   # list of step dicts
                    "tool_calls": [...],    # list of tool call dicts
                },
                ...
            ],
            "next_cursor": str | None,
        }

    Each **model_call** dict has keys: ``step``, ``model``, ``fast_path``,
    ``tools_offered``, ``thinking_text``, ``content_text``, ``duration_ms``.

    Each **tool_call** dict has keys: ``seq``, ``tool``, ``args_json``,
    ``status``, ``error_code``, ``duration_ms``.
    """
    params: list = []
    where = ""
    if cursor is not None:
        where = "WHERE r.ts_start < ?"
        params.append(cursor)

    # Fetch limit+1 to know whether there is a next page.
    sql = (
        f"SELECT r.request_id, r.ts_start, r.channel, {_derived_path('r')} AS path, r.outcome, "
        "       r.duration_ms, r.ttft_ms, r.input_text, r.output_text "
        f"FROM requests r {where} "
        "ORDER BY r.ts_start DESC "
        "LIMIT ?"
    )
    params.append(limit + 1)
    raw_rows = conn.execute(sql, params).fetchall()

    has_more = len(raw_rows) > limit
    raw_rows = raw_rows[:limit]

    def _model_calls(request_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT step, model, fast_path, tools_offered, thinking_text, "
            "       content_text, duration_ms "
            "FROM model_calls WHERE request_id = ? ORDER BY step",
            (request_id,),
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "step": row[0],
                "model": row[1],
                "fast_path": bool(row[2]),
                "tools_offered": row[3],
                "thinking_text": row[4],
                "content_text": row[5],
                "duration_ms": row[6],
            })
        return result

    def _tool_calls(request_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT seq, tool, args_json, status, error_code, duration_ms "
            "FROM tool_calls WHERE request_id = ? ORDER BY seq",
            (request_id,),
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "seq": row[0],
                "tool": row[1],
                "args_json": row[2],
                "status": row[3],
                "error_code": row[4],
                "duration_ms": row[5],
            })
        return result

    result_rows = []
    for row in raw_rows:
        request_id = row[0]
        result_rows.append({
            "request_id": request_id,
            "ts_start": row[1],
            "channel": row[2],
            "path": row[3],
            "outcome": row[4],
            "duration_ms": row[5],
            "ttft_ms": row[6],
            "input_text": row[7],
            "output_text": row[8],
            "model_calls": _model_calls(request_id),
            "tool_calls": _tool_calls(request_id),
        })

    next_cursor = result_rows[-1]["ts_start"] if has_more and result_rows else None

    return {
        "rows": result_rows,
        "next_cursor": next_cursor,
    }


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
