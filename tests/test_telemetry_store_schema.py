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
