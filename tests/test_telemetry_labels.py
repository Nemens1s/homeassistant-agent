"""Tests for Task 16: labels store write and POST /api/labels endpoint."""

import pytest

from app.telemetry.store import open_store, insert_label


# ---------------------------------------------------------------------------
# Unit test: store.insert_label
# ---------------------------------------------------------------------------


def test_insert_label(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    conn.execute(
        "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
        "VALUES ('r1','2026-09-17T00:00:00Z','ui','hi','agent','ok')"
    )
    conn.commit()
    lid = insert_label(conn, "r1", source="ui", rating=-1, note="wrong")
    row = conn.execute(
        "SELECT request_id, source, rating, note FROM labels WHERE id=?", (lid,)
    ).fetchone()
    assert row == ("r1", "ui", -1, "wrong")


def test_insert_label_all_optional_fields(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    conn.execute(
        "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
        "VALUES ('r2','2026-09-17T00:00:00Z','ui','hi','agent','ok')"
    )
    conn.commit()
    lid = insert_label(
        conn,
        "r2",
        source="analyst",
        rating=1,
        correct_tool="get_entity_state",
        correct_entity_id="light.living_room",
        note="correct",
    )
    row = conn.execute(
        "SELECT request_id, source, rating, correct_tool, correct_entity_id, note "
        "FROM labels WHERE id=?",
        (lid,),
    ).fetchone()
    assert row == ("r2", "analyst", 1, "get_entity_state", "light.living_room", "correct")


def test_insert_label_ts_is_set(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    conn.execute(
        "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
        "VALUES ('r3','2026-09-17T00:00:00Z','ui','hi','agent','ok')"
    )
    conn.commit()
    lid = insert_label(conn, "r3", source="ui")
    row = conn.execute("SELECT ts FROM labels WHERE id=?", (lid,)).fetchone()
    assert row is not None
    ts = row[0]
    # Must be a non-empty ISO-8601 string ending in Z
    assert isinstance(ts, str) and ts.endswith("Z") and len(ts) > 10


# ---------------------------------------------------------------------------
# Endpoint tests: POST /api/labels
# ---------------------------------------------------------------------------


@pytest.fixture()
def reset_otel_provider():
    """Reset the OTel global TracerProvider before and after the test.

    Mirrors the fixture in test_telemetry_request_span.py but also resets
    the ``_TRACER_PROVIDER_SET_ONCE`` latch, which is required when this file
    runs before test_telemetry_request_span.py (alphabetically it does).
    Without resetting the latch, subsequent ``trace.set_tracer_provider`` calls
    silently no-op and the second test ends up with a no-op tracer.
    """
    from opentelemetry import trace as otel_trace

    _orig = otel_trace._TRACER_PROVIDER  # noqa: SLF001
    _orig_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # noqa: SLF001

    otel_trace._TRACER_PROVIDER = None  # noqa: SLF001
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001

    yield

    otel_trace._TRACER_PROVIDER = _orig  # noqa: SLF001
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = _orig_done  # noqa: SLF001


def _telemetry_settings(tmp_path):
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


def test_post_labels_inserts_row(tmp_path, reset_otel_provider):
    """POST /api/labels inserts into labels and returns {"id": <int>}."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = _telemetry_settings(tmp_path)
    app = create_app(settings)

    with TestClient(app) as client:
        # Insert a parent request row so the FK is satisfied.
        conn = app.state.telemetry_store
        assert conn is not None
        conn.execute(
            "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
            "VALUES ('abc123','2026-09-17T00:00:00Z','ui','hi','agent','ok')"
        )
        conn.commit()

        resp = client.post(
            "/api/labels",
            json={"request_id": "abc123", "source": "ui", "rating": 1, "note": "good"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "id" in body
        assert isinstance(body["id"], int)

        # Verify the row landed in the DB — must be inside `with` block because
        # the lifespan closes the connection on exit.
        row = conn.execute(
            "SELECT request_id, source, rating, note FROM labels WHERE id=?",
            (body["id"],),
        ).fetchone()
        assert row == ("abc123", "ui", 1, "good")


def test_post_labels_503_when_telemetry_disabled():
    """POST /api/labels returns HTTP 503 when telemetry is disabled."""
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
        resp = client.post(
            "/api/labels",
            json={"request_id": "doesnotmatter", "source": "ui"},
        )

    assert resp.status_code == 503
