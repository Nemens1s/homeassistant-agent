"""Tests for Task 17: read queries, derived views, and telemetry API endpoints."""
from __future__ import annotations

import sqlite3
import pytest

from app.telemetry.store import open_store, summary, recent_requests


# ---------------------------------------------------------------------------
# TDD anchor: summary counts requests by path
# ---------------------------------------------------------------------------


def test_summary_counts_requests_by_path(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    for rid, path in [("r1", "fast_path"), ("r2", "agent"), ("r3", "fast_path")]:
        conn.execute(
            "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
            "VALUES (?, '2026-09-17T00:00:00Z','ui','hi',?, 'ok')",
            (rid, path),
        )
    conn.commit()
    s = summary(conn, days=3650)
    assert s["requests_by_path"]["fast_path"] == 2
    assert s["requests_by_path"]["agent"] == 1


# ---------------------------------------------------------------------------
# summary: outcome counts
# ---------------------------------------------------------------------------


def test_summary_outcome_counts(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    for rid, outcome in [("r1", "ok"), ("r2", "ok"), ("r3", "error"), ("r4", "recursion_limit")]:
        conn.execute(
            "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
            "VALUES (?, '2026-09-17T00:00:00Z','ui','hi','agent',?)",
            (rid, outcome),
        )
    conn.commit()
    s = summary(conn, days=3650)
    counts = s["outcome_counts"]
    assert counts["ok"] == 2
    assert counts["error"] == 1
    assert counts["recursion_limit"] == 1


# ---------------------------------------------------------------------------
# summary: fast-path hit rate
# ---------------------------------------------------------------------------


def test_summary_fast_path_hit_rate(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    for rid, path in [("r1", "fast_path"), ("r2", "fast_path"), ("r3", "agent"), ("r4", "agent")]:
        conn.execute(
            "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
            "VALUES (?, '2026-09-17T00:00:00Z','ui','hi',?, 'ok')",
            (rid, path),
        )
    conn.commit()
    s = summary(conn, days=3650)
    assert s["fast_path_hit_rate"] == pytest.approx(0.5)


def test_summary_fast_path_hit_rate_no_requests(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    s = summary(conn, days=3650)
    assert s["fast_path_hit_rate"] is None


# ---------------------------------------------------------------------------
# summary: label counts
# ---------------------------------------------------------------------------


def test_summary_label_counts(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    conn.execute(
        "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
        "VALUES ('r1','2026-09-17T00:00:00Z','ui','hi','agent','ok')"
    )
    conn.commit()
    from app.telemetry.store import insert_label
    insert_label(conn, "r1", source="ui", rating=1)
    insert_label(conn, "r1", source="ui", rating=-1)
    insert_label(conn, "r1", source="analyst")
    s = summary(conn, days=3650)
    lc = s["label_counts"]
    assert lc["total"] == 3
    assert lc["thumbs_up"] == 1
    assert lc["thumbs_down"] == 1


# ---------------------------------------------------------------------------
# summary: duration percentiles (may be None with no data)
# ---------------------------------------------------------------------------


def test_summary_has_duration_percentiles_keys(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    s = summary(conn, days=3650)
    assert "duration_p50_by_path" in s
    assert "duration_p95_by_path" in s
    assert "ttft_p50_by_path" in s
    assert "ttft_p95_by_path" in s


# ---------------------------------------------------------------------------
# v1 → v2 migration test
# ---------------------------------------------------------------------------


def test_v1_to_v2_migration_creates_views(tmp_path):
    """Open a v1 store, verify it's at version 1, then re-open to apply v2."""
    import importlib
    import unittest.mock as mock

    # Create a v1 store by temporarily patching SCHEMA_VERSION to 1.
    with mock.patch("app.telemetry.store.SCHEMA_VERSION", 1):
        # Also temporarily shorten _MIGRATIONS so only v1 applies.
        import app.telemetry.store as store_mod
        original_migrations = store_mod._MIGRATIONS[:]
        store_mod._MIGRATIONS = store_mod._MIGRATIONS[:1]
        try:
            conn_v1 = open_store(str(tmp_path / "t.sqlite"))
            # Insert a request row to verify data survives migration.
            conn_v1.execute(
                "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
                "VALUES ('r1','2026-09-17T00:00:00Z','ui','hi','agent','ok')"
            )
            conn_v1.commit()
            v = conn_v1.execute("SELECT version FROM schema_version").fetchone()[0]
            assert v == 1
            conn_v1.close()
        finally:
            store_mod._MIGRATIONS = original_migrations

    # Now open again with full SCHEMA_VERSION=2 — migration to v2 should apply.
    conn_v2 = open_store(str(tmp_path / "t.sqlite"))
    v2 = conn_v2.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v2 == 2

    # All three views should exist.
    views = {
        row[0]
        for row in conn_v2.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()
    }
    assert "fast_path_dataset" in views
    assert "tool_offer_stats" in views
    assert "request_quality" in views

    # Original data survived.
    row = conn_v2.execute("SELECT request_id FROM requests WHERE request_id='r1'").fetchone()
    assert row is not None

    conn_v2.close()


def test_v2_migration_idempotent(tmp_path):
    """Opening a v2 store a second time must not error."""
    conn = open_store(str(tmp_path / "t.sqlite"))
    conn.close()
    # Re-open — should not raise.
    conn2 = open_store(str(tmp_path / "t.sqlite"))
    conn2.close()


# ---------------------------------------------------------------------------
# recent_requests: basic shape
# ---------------------------------------------------------------------------


def test_recent_requests_basic(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    for rid, ts in [("r1", "2026-09-17T00:00:01Z"), ("r2", "2026-09-17T00:00:02Z")]:
        conn.execute(
            "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
            "VALUES (?, ?, 'ui', 'hi', 'agent', 'ok')",
            (rid, ts),
        )
    conn.commit()
    result = recent_requests(conn, limit=10)
    assert "rows" in result
    assert "next_cursor" in result
    # newest first
    assert result["rows"][0]["request_id"] == "r2"
    assert result["rows"][1]["request_id"] == "r1"


def test_recent_requests_pagination(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    for i in range(5):
        conn.execute(
            "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
            "VALUES (?, ?, 'ui', 'hi', 'agent', 'ok')",
            (f"r{i}", f"2026-09-17T00:00:0{i}Z"),
        )
    conn.commit()
    page1 = recent_requests(conn, limit=3)
    assert len(page1["rows"]) == 3
    assert page1["next_cursor"] is not None

    page2 = recent_requests(conn, limit=3, cursor=page1["next_cursor"])
    assert len(page2["rows"]) == 2
    assert page2["next_cursor"] is None


# ---------------------------------------------------------------------------
# Endpoint tests: GET /api/telemetry/summary and GET /api/telemetry/requests
# ---------------------------------------------------------------------------


def _make_telemetry_settings(tmp_path):
    from app.config import Settings

    return Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        telemetry_enabled=True,
        telemetry_db_path=str(tmp_path / "telemetry.sqlite"),
    )


def test_get_telemetry_summary_200(tmp_path, reset_otel_provider):
    """GET /api/telemetry/summary returns 200 with a dict when telemetry is on."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings = _make_telemetry_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        resp = client.get("/api/telemetry/summary?days=7")

    assert resp.status_code == 200
    body = resp.json()
    assert "requests_by_path" in body


def test_get_telemetry_summary_503_when_disabled():
    """GET /api/telemetry/summary returns 503 when telemetry is disabled."""
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        telemetry_enabled=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        resp = client.get("/api/telemetry/summary?days=7")

    assert resp.status_code == 503


def test_get_telemetry_requests_200(tmp_path, reset_otel_provider):
    """GET /api/telemetry/requests returns 200 with rows + next_cursor."""
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings = _make_telemetry_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        resp = client.get("/api/telemetry/requests?limit=10")

    assert resp.status_code == 200
    body = resp.json()
    assert "rows" in body
    assert "next_cursor" in body


def test_get_telemetry_requests_503_when_disabled():
    """GET /api/telemetry/requests returns 503 when telemetry is disabled."""
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
        telemetry_enabled=False,
    )
    app = create_app(settings)

    with TestClient(app) as client:
        resp = client.get("/api/telemetry/requests?limit=10")

    assert resp.status_code == 503
