import sqlite3
from datetime import datetime, timedelta, timezone

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.config import Settings
from app.i18n.store import LangOverlay
from app.retention import RetentionJob, ThreadActivity, prune_telemetry, prune_threads
from app.telemetry.store import insert_label, open_store

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _request(conn, request_id, ts):
    conn.execute(
        "INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
        "VALUES (?, ?, 'ui', 'hi', 'agent', 'ok')",
        (request_id, ts.isoformat()),
    )
    conn.execute(
        "INSERT INTO tool_calls (span_id, request_id, seq, tool) VALUES (?, ?, 0, 't')",
        ("span-" + request_id, request_id),
    )
    conn.commit()


def _ids(conn, table):
    rows = conn.execute(f"SELECT request_id FROM {table} ORDER BY request_id").fetchall()
    return [row[0] for row in rows]


def test_prune_telemetry_drops_old_unlabelled_requests_with_children(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    _request(conn, "old", NOW - timedelta(days=40))
    _request(conn, "old_labelled", NOW - timedelta(days=40))
    _request(conn, "recent", NOW - timedelta(days=1))
    insert_label(conn, request_id="old_labelled", source="ui", rating=1)

    assert prune_telemetry(conn, days=30, now=NOW) == 1
    assert _ids(conn, "requests") == ["old_labelled", "recent"]
    assert _ids(conn, "tool_calls") == ["old_labelled", "recent"]
    conn.close()


def test_prune_telemetry_zero_days_keeps_everything(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    _request(conn, "ancient", NOW - timedelta(days=4000))
    assert prune_telemetry(conn, days=0, now=NOW) == 0
    assert _ids(conn, "requests") == ["ancient"]
    conn.close()


class _State(TypedDict, total=False):
    n: int


def _graph(saver):
    graph = StateGraph(_State)
    graph.add_node("step", lambda state: {"n": state.get("n", 0) + 1})
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph.compile(checkpointer=saver)


async def _turn(graph, thread_id):
    await graph.ainvoke({"n": 0}, config={"configurable": {"thread_id": thread_id}})


def _threads(path):
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id").fetchall()
    conn.close()
    return [row[0] for row in rows]


async def test_prune_threads_deletes_only_threads_idle_past_the_cutoff(tmp_path):
    path = str(tmp_path / "checkpoints.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = _graph(saver)
        await _turn(graph, "idle")
        await _turn(graph, "busy")
        overlay = LangOverlay(path)
        overlay.record("idle", "et", "tere", "hello", "hi", "tere")
        activity = ThreadActivity(path)

        # First sighting: both threads count as active from NOW.
        assert await prune_threads(saver, activity, overlay, days=30, now=NOW) == 0

        await _turn(graph, "busy")  # new checkpoint on one thread only
        later = NOW + timedelta(days=31)
        assert await prune_threads(saver, activity, overlay, days=30, now=later) == 1

        assert _threads(path) == ["busy"]
        assert overlay.rows_for("idle") == []
        activity.close()
        overlay.close()


async def test_prune_threads_zero_days_is_a_no_op(tmp_path):
    path = str(tmp_path / "checkpoints.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        await _turn(_graph(saver), "a")
        activity = ThreadActivity(path)
        far = NOW + timedelta(days=4000)
        assert await prune_threads(saver, activity, None, days=0, now=far) == 0
        assert _threads(path) == ["a"]
        activity.close()


async def test_retention_job_survives_a_broken_telemetry_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.sqlite"))  # no telemetry schema
    job = RetentionJob(Settings(_env_file=None), telemetry_conn=conn)
    await job.run_once(NOW)  # logs a warning, never raises
    conn.close()
