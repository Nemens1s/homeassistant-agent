from app.telemetry.store import open_store, SCHEMA_VERSION


def test_open_store_creates_all_tables(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"requests", "model_calls", "tool_calls", "fast_path_decisions",
            "labels", "prompt_snapshots", "toolset_snapshots", "menu_snapshots"} <= names
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION


def test_migrations_are_idempotent(tmp_path):
    p = str(tmp_path / "t.sqlite")
    open_store(p).close()
    conn = open_store(p)  # second open must not fail or duplicate
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_wal_mode(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_model_calls_has_templated_reply_column(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(model_calls)")]
    conn.close()
    assert "templated_reply" in columns


def test_tool_offer_stats_skips_templated_rows(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    conn.execute("INSERT INTO toolset_snapshots VALUES ('h1', '[]', '2026-01-01')")
    conn.execute(
        "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome, toolset_hash) "
        "VALUES ('r1', '2026-01-01', 'chat', 'x', 'agent', 'ok', 'h1')")
    for span_id, templated in (("s1", 0), ("s2", 1)):
        conn.execute(
            "INSERT INTO model_calls (span_id, request_id, step, ts_start, tools_offered, "
            "templated_reply) VALUES (?, 'r1', 1, '2026-01-01', '[\"get_weather\"]', ?)",
            (span_id, templated))
    offered = conn.execute(
        "SELECT times_offered FROM tool_offer_stats WHERE tool = 'get_weather'").fetchone()[0]
    conn.close()
    assert offered == 1
