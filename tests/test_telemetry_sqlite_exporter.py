"""
Tests for SqliteSpanExporter — TDD step 1 (failing), then step 4 (passing).

Out-of-order test: a child span (model_calls) is exported BEFORE its parent
request span, verifying no row is silently dropped due to FK ordering.
"""

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind

from app.telemetry.store import open_store
from app.telemetry.sqlite_exporter import SqliteSpanExporter
from app.telemetry import conventions as c


def _provider(conn):
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(SqliteSpanExporter(conn)))
    return tp


# ---------------------------------------------------------------------------
# Canonical test from the brief
# ---------------------------------------------------------------------------

def test_chat_span_maps_to_model_calls_row(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    tp = _provider(conn)
    tracer = tp.get_tracer("test")
    with tracer.start_as_current_span(c.SPAN_INVOKE_AGENT) as root:
        root.set_attribute(c.GOSLING_INPUT_TEXT, "goodnight")
        root.set_attribute(c.GOSLING_PATH, "fast_path")
        root.set_attribute(c.GOSLING_OUTCOME, "ok")
        with tracer.start_as_current_span(f"{c.SPAN_CHAT} needle") as chat:
            chat.set_attribute(c.GOSLING_FAST_PATH, True)
            chat.set_attribute(c.GOSLING_STEP, 1)
            chat.set_attribute(c.GOSLING_TOOLS_OFFERED, '["trigger_automation"]')
    tp.shutdown()
    rows = conn.execute("SELECT fast_path, step, tools_offered FROM model_calls").fetchall()
    assert rows == [(1, 1, '["trigger_automation"]')]
    reqs = conn.execute("SELECT input_text, path, outcome FROM requests").fetchall()
    assert reqs == [("goodnight", "fast_path", "ok")]


# ---------------------------------------------------------------------------
# Root-only request
# ---------------------------------------------------------------------------

def test_root_span_maps_to_requests_row(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    tp = _provider(conn)
    tracer = tp.get_tracer("test")
    with tracer.start_as_current_span(c.SPAN_INVOKE_AGENT) as root:
        root.set_attribute(c.GOSLING_INPUT_TEXT, "hello")
        root.set_attribute(c.GOSLING_PATH, "agent")
        root.set_attribute(c.GOSLING_OUTCOME, "ok")
        root.set_attribute(c.GOSLING_CHANNEL, "ui")
    tp.shutdown()
    reqs = conn.execute("SELECT input_text, path, outcome, channel FROM requests").fetchall()
    assert reqs == [("hello", "agent", "ok", "ui")]


# ---------------------------------------------------------------------------
# Out-of-order export: child exported BEFORE parent request
#
# We simulate the real-world scenario where BatchSpanProcessor exports a chat
# span in one batch and the parent invoke_agent span in a later batch.
# We call exporter.export() directly with hand-crafted spans so we can control
# the order.
# ---------------------------------------------------------------------------

def test_out_of_order_child_before_parent(tmp_path):
    """
    Export a chat (model_calls) span first, then the parent request span.
    Both rows must land with correct values; no FK violation must drop a row.
    """
    import time
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import SpanContext, TraceFlags
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan as RS

    conn = open_store(str(tmp_path / "t.sqlite"))
    exporter = SqliteSpanExporter(conn)

    # Build trace/span ids manually so we control them
    tracer_provider = TracerProvider(resource=Resource.create({}))
    tracer = tracer_provider.get_tracer("ooo-test")

    # We capture spans by recording them with a SimpleSpanProcessor that
    # calls our exporter, but in a controlled order via two separate export()
    # calls.

    # Use real spans from a nested context so parent/child context is correct,
    # but we intercept them before they are exported.
    captured = []

    class CapturingExporter:
        def export(self, spans):
            captured.extend(spans)
            return True
        def shutdown(self):
            pass

    capturing_processor = SimpleSpanProcessor(CapturingExporter())
    tracer_provider.add_span_processor(capturing_processor)

    with tracer.start_as_current_span(c.SPAN_INVOKE_AGENT) as root:
        root.set_attribute(c.GOSLING_INPUT_TEXT, "out of order")
        root.set_attribute(c.GOSLING_PATH, "agent")
        root.set_attribute(c.GOSLING_OUTCOME, "ok")
        root.set_attribute(c.GOSLING_CHANNEL, "cli")
        with tracer.start_as_current_span(f"{c.SPAN_CHAT} step1") as chat:
            chat.set_attribute(c.GOSLING_FAST_PATH, False)
            chat.set_attribute(c.GOSLING_STEP, 1)
            chat.set_attribute(c.GOSLING_TOOLS_OFFERED, "[]")

    tracer_provider.shutdown()

    # captured[0] = chat span (ended first), captured[1] = root span
    assert len(captured) == 2
    chat_span = captured[0]
    root_span = captured[1]
    assert chat_span.name.startswith(c.SPAN_CHAT)
    assert root_span.name == c.SPAN_INVOKE_AGENT

    from opentelemetry.sdk.trace.export import SpanExportResult

    # Export child FIRST — this is the out-of-order scenario
    result1 = exporter.export([chat_span])
    assert result1 == SpanExportResult.SUCCESS

    # Now export parent — stub must be promoted to full row
    result2 = exporter.export([root_span])
    assert result2 == SpanExportResult.SUCCESS

    # Both rows must exist and carry correct values
    mc_rows = conn.execute(
        "SELECT step, tools_offered, fast_path FROM model_calls"
    ).fetchall()
    assert mc_rows == [(1, "[]", 0)]

    req_rows = conn.execute(
        "SELECT input_text, path, outcome, channel FROM requests"
    ).fetchall()
    assert req_rows == [("out of order", "agent", "ok", "cli")]

    # Verify stub was promoted: no NULL in required columns
    full = conn.execute(
        "SELECT input_text, path, outcome FROM requests"
    ).fetchone()
    assert full == ("out of order", "agent", "ok")


# ---------------------------------------------------------------------------
# execute_tool span → tool_calls
# ---------------------------------------------------------------------------

def test_execute_tool_span_maps_to_tool_calls_row(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    tp = _provider(conn)
    tracer = tp.get_tracer("test")
    with tracer.start_as_current_span(c.SPAN_INVOKE_AGENT) as root:
        root.set_attribute(c.GOSLING_INPUT_TEXT, "do something")
        root.set_attribute(c.GOSLING_PATH, "agent")
        root.set_attribute(c.GOSLING_OUTCOME, "ok")
        with tracer.start_as_current_span(f"{c.SPAN_EXECUTE_TOOL} get_state") as tool:
            tool.set_attribute(c.GEN_AI_TOOL_NAME, "get_state")
            tool.set_attribute(c.GEN_AI_TOOL_CALL_ID, "call_123")
            tool.set_attribute(c.GOSLING_TOOL_ARGS, '{"entity_id": "light.office"}')
            tool.set_attribute(c.GOSLING_TOOL_STATUS, "ok")
            tool.set_attribute(c.GOSLING_TOOL_RESULT, '{"state": "on"}')
    tp.shutdown()
    rows = conn.execute(
        "SELECT tool, call_id, args_json, status, result_text FROM tool_calls"
    ).fetchall()
    assert rows == [("get_state", "call_123", '{"entity_id": "light.office"}', "ok", '{"state": "on"}')]


# ---------------------------------------------------------------------------
# Tool result cap at 64 KB
# ---------------------------------------------------------------------------

def test_tool_result_capped_at_65536_chars(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    tp = _provider(conn)
    tracer = tp.get_tracer("test")
    big_result = "x" * 100_000
    with tracer.start_as_current_span(c.SPAN_INVOKE_AGENT) as root:
        root.set_attribute(c.GOSLING_INPUT_TEXT, "big")
        root.set_attribute(c.GOSLING_PATH, "agent")
        root.set_attribute(c.GOSLING_OUTCOME, "ok")
        with tracer.start_as_current_span(f"{c.SPAN_EXECUTE_TOOL} big_tool") as tool:
            tool.set_attribute(c.GEN_AI_TOOL_NAME, "big_tool")
            tool.set_attribute(c.GOSLING_TOOL_RESULT, big_result)
    tp.shutdown()
    row = conn.execute("SELECT result_text FROM tool_calls").fetchone()
    assert row is not None
    assert len(row[0]) == 65536


# ---------------------------------------------------------------------------
# fast_path.classify span → fast_path_decisions
# ---------------------------------------------------------------------------

def test_classify_span_maps_to_fast_path_decisions(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    tp = _provider(conn)
    tracer = tp.get_tracer("test")
    with tracer.start_as_current_span(c.SPAN_INVOKE_AGENT) as root:
        root.set_attribute(c.GOSLING_INPUT_TEXT, "turn off lights")
        root.set_attribute(c.GOSLING_PATH, "fast_path")
        root.set_attribute(c.GOSLING_OUTCOME, "ok")
        with tracer.start_as_current_span(c.SPAN_CLASSIFY) as classify:
            classify.set_attribute(c.GOSLING_FP_BACKEND, "needle")
            classify.set_attribute(c.GOSLING_FP_ENTITY_ID, "light.office")
            classify.set_attribute(c.GOSLING_FP_CONFIDENCE, 0.95)
            classify.set_attribute(c.GOSLING_FP_THRESHOLD, 0.8)
            classify.set_attribute(c.GOSLING_FP_ACCEPTED, True)
    tp.shutdown()
    rows = conn.execute(
        "SELECT backend, entity_id, confidence, threshold, accepted FROM fast_path_decisions"
    ).fetchall()
    assert rows == [("needle", "light.office", 0.95, 0.8, 1)]
