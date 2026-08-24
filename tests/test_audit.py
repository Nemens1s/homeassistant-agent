import asyncio
import sqlite3

from app.audit import AuditSink


async def test_disabled_sink_is_noop(tmp_path):
    sink = AuditSink("")
    await sink.record(thread_id="t", tool="control_entity", entity_id="light.k",
                      domain="light", service="turn_off", params_json="{}",
                      status="ok", error_code=None, duration_ms=5)
    sink.close()  # nothing raised, nothing created


async def test_record_writes_row(tmp_path):
    db = tmp_path / "audit.db"
    sink = AuditSink(str(db))
    await sink.record(thread_id="cli", tool="control_entity", entity_id="light.kitchen",
                      domain="light", service="turn_off", params_json='{"a":1}',
                      status="ok", error_code=None, duration_ms=42)
    await sink.record(thread_id="cli", tool="control_entity", entity_id="lock.front",
                      domain="lock", service="unlock", params_json="{}",
                      status="error", error_code="domain_not_allowed", duration_ms=1)
    sink.close()
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT thread_id, tool, entity_id, domain, service, status, error_code, duration_ms "
        "FROM actions ORDER BY id"
    ).fetchall()
    assert rows[0] == ("cli", "control_entity", "light.kitchen", "light", "turn_off", "ok", None, 42)
    assert rows[1][6] == "domain_not_allowed"
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()


async def test_concurrent_writes_both_land(tmp_path):
    db = tmp_path / "audit.db"
    sink = AuditSink(str(db))
    await asyncio.gather(
        sink.record(thread_id="t1", tool="control_entity", entity_id="light.a",
                    domain="light", service="turn_on", params_json="{}",
                    status="ok", error_code=None, duration_ms=10),
        sink.record(thread_id="t2", tool="control_entity", entity_id="light.b",
                    domain="light", service="turn_off", params_json="{}",
                    status="ok", error_code=None, duration_ms=20),
    )
    sink.close()
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    conn.close()
    assert count == 2


async def test_record_never_raises(tmp_path, caplog):
    sink = AuditSink(str(tmp_path / "audit.db"))
    sink._conn.close()  # sabotage the connection
    with caplog.at_level("ERROR", logger="agent.audit"):
        await sink.record(thread_id="t", tool="x", entity_id="", domain="", service="",
                          params_json="{}", status="ok", error_code=None, duration_ms=0)
    assert any("audit write failed" in r.getMessage() for r in caplog.records)
