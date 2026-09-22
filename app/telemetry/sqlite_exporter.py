"""
SqliteSpanExporter — maps OTel spans to dataset rows.

Span name routing:
  "invoke_agent gosling"  →  requests
  "chat *"                →  model_calls
  "fast_path.classify"    →  fast_path_decisions
  "execute_tool *"        →  tool_calls

FK-ordering strategy (stub approach)
-------------------------------------
open_store() enables PRAGMA foreign_keys=ON.  Spans end in reverse nesting
order: child spans (chat, execute_tool, classify) finish BEFORE their parent
invoke_agent span.  With BatchSpanProcessor a batch of children can arrive
before any batch containing the parent, which would cause INSERT of a child
row to violate the FK constraint  requests(request_id)  and silently drop
the row (the per-span try/except swallows it).

Solution: before inserting any child row whose parent request may not yet
exist, we first execute:

    INSERT OR IGNORE INTO requests (request_id) VALUES (trace_id)

This inserts a minimal stub row (all other columns NULL).  The stub satisfies
the FK requirement so the child row can be inserted.  Later, when the real
invoke_agent span is exported, INSERT OR REPLACE writes all columns — the
stub's NULL columns are replaced by real data.  Because INSERT OR REPLACE
always carries all column values (never omits them), the stub cannot silently
clobber real data.

Within a single export() call that contains BOTH a root span and child spans
we still insert the root first to avoid creating unnecessary stubs.
"""

import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.telemetry import conventions as c

_log = logging.getLogger("telemetry")

_TOOL_RESULT_CAP = 65536


def _ts(ns: int) -> str:
    """Convert nanosecond timestamp to UTC ISO-8601 string."""
    dt = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
    return dt.isoformat()


def _duration_ms(start_ns: int, end_ns: int) -> int:
    return int((end_ns - start_ns) / 1e6)


def _attr(span: ReadableSpan, key: str):
    """Return a span attribute or None if absent."""
    if span.attributes is None:
        return None
    return span.attributes.get(key)


def _bool_to_int(val) -> int | None:
    if val is None:
        return None
    return 1 if val else 0


class SqliteSpanExporter(SpanExporter):
    """Exports OTel spans to the agent telemetry SQLite dataset."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # SpanExporter interface
    # ------------------------------------------------------------------

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            self._export_batch(spans)
        except Exception:
            _log.exception("SqliteSpanExporter: fatal error writing batch")
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass  # connection lifecycle is managed by the caller

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _export_batch(self, spans: Sequence[ReadableSpan]) -> None:
        # Sort so invoke_agent (root) spans come first within the batch.
        # This avoids creating stubs when parent and children arrive together.
        def sort_key(span: ReadableSpan) -> int:
            if span.name == c.SPAN_INVOKE_AGENT:
                return 0
            return 1

        ordered = sorted(spans, key=sort_key)

        for span in ordered:
            try:
                self._export_one(span)
            except Exception:
                _log.exception(
                    "SqliteSpanExporter: error mapping span name=%r", span.name
                )

    def _export_one(self, span: ReadableSpan) -> None:
        name = span.name
        if name == c.SPAN_INVOKE_AGENT:
            self._write_request(span)
        elif name.startswith(c.SPAN_CHAT + " ") or name == c.SPAN_CHAT:
            self._ensure_request_stub(span)
            self._write_model_call(span)
        elif name == c.SPAN_CLASSIFY:
            self._ensure_request_stub(span)
            self._write_fast_path_decision(span)
        elif name.startswith(c.SPAN_EXECUTE_TOOL + " ") or name == c.SPAN_EXECUTE_TOOL:
            self._ensure_request_stub(span)
            self._write_tool_call(span)
        # Unknown span names are silently ignored (no mapping defined)

    def _trace_id(self, span: ReadableSpan) -> str:
        return format(span.context.trace_id, "032x")

    def _span_id(self, span: ReadableSpan) -> str:
        return format(span.context.span_id, "016x")

    def _ensure_request_stub(self, span: ReadableSpan) -> None:
        """Insert a minimal stub requests row if one does not already exist.

        Uses INSERT OR IGNORE so it is a no-op when the real row already
        exists (inserted by _write_request earlier in the same batch, or
        from a previous export call).

        The NOT NULL columns (channel, input_text, path, outcome, ts_start)
        receive empty-string / placeholder values so SQLite accepts the row.
        INSERT OR REPLACE for the real invoke_agent span overwrites all of
        them with correct data later.
        """
        trace_id = self._trace_id(span)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO requests
                (request_id, ts_start, channel, input_text, path, outcome)
            VALUES (?, '', '', '', '', '')
            """,
            (trace_id,),
        )
        self._conn.commit()

    def _write_request(self, span: ReadableSpan) -> None:
        trace_id = self._trace_id(span)
        ts_start = _ts(span.start_time)
        duration = _duration_ms(span.start_time, span.end_time) if span.end_time else None

        self._conn.execute(
            """
            INSERT OR REPLACE INTO requests (
                request_id, ts_start, duration_ms,
                channel, endpoint, device_id, thread_id,
                input_text, output_text,
                path, outcome, error,
                model, provider, app_version, max_tier,
                prompt_hash, toolset_hash,
                steps, ttft_ms
            ) VALUES (
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?
            )
            """,
            (
                trace_id,
                ts_start,
                duration,
                # channel is NOT NULL in the schema; fall back to empty string
                _attr(span, c.GOSLING_CHANNEL) or "",
                _attr(span, c.GOSLING_ENDPOINT),
                _attr(span, c.GOSLING_DEVICE_ID),
                # thread_id: no convention key defined; leave NULL
                None,
                # input_text is NOT NULL
                _attr(span, c.GOSLING_INPUT_TEXT) or "",
                _attr(span, c.GOSLING_OUTPUT_TEXT),
                # path is NOT NULL
                _attr(span, c.GOSLING_PATH) or "",
                # outcome is NOT NULL
                _attr(span, c.GOSLING_OUTCOME) or "",
                None,  # error: no dedicated convention key; use None
                _attr(span, c.GEN_AI_REQUEST_MODEL),
                _attr(span, c.GOSLING_LLM_PROVIDER),
                _attr(span, c.GOSLING_APP_VERSION),
                _attr(span, c.GOSLING_MAX_TIER),
                _attr(span, c.GOSLING_PROMPT_HASH),
                _attr(span, c.GOSLING_TOOLSET_HASH),
                None,  # steps: not stored in span attributes
                _attr(span, c.GOSLING_TTFT_MS),
            ),
        )
        self._conn.commit()

    def _write_model_call(self, span: ReadableSpan) -> None:
        span_id = self._span_id(span)
        trace_id = self._trace_id(span)
        ts_start = _ts(span.start_time)
        duration = _duration_ms(span.start_time, span.end_time) if span.end_time else None
        fast_path_val = _attr(span, c.GOSLING_FAST_PATH)

        self._conn.execute(
            """
            INSERT OR REPLACE INTO model_calls (
                span_id, request_id, step, ts_start, duration_ms,
                model, fast_path, tools_offered, messages_count,
                input_tokens, output_tokens,
                thinking_text, content_text, tool_calls, finish_reason
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                span_id,
                trace_id,
                _attr(span, c.GOSLING_STEP) or 0,
                ts_start,
                duration,
                _attr(span, c.GEN_AI_REQUEST_MODEL),
                _bool_to_int(fast_path_val) if fast_path_val is not None else 0,
                _attr(span, c.GOSLING_TOOLS_OFFERED) or "[]",
                _attr(span, c.GOSLING_MESSAGES_COUNT),
                _attr(span, c.GEN_AI_USAGE_INPUT_TOKENS),
                _attr(span, c.GEN_AI_USAGE_OUTPUT_TOKENS),
                _attr(span, c.GOSLING_THINKING_TEXT),
                _attr(span, c.GOSLING_CONTENT_TEXT),
                _attr(span, c.GOSLING_TOOL_CALLS),
                _attr(span, c.GEN_AI_RESPONSE_FINISH_REASONS),
            ),
        )
        self._conn.commit()

    def _write_tool_call(self, span: ReadableSpan) -> None:
        span_id = self._span_id(span)
        trace_id = self._trace_id(span)
        duration = _duration_ms(span.start_time, span.end_time) if span.end_time else None

        raw_result = _attr(span, c.GOSLING_TOOL_RESULT)
        if raw_result is not None and len(raw_result) > _TOOL_RESULT_CAP:
            raw_result = raw_result[:_TOOL_RESULT_CAP]

        self._conn.execute(
            """
            INSERT OR REPLACE INTO tool_calls (
                span_id, request_id, seq, tool, call_id,
                args_json, status, error_code, duration_ms, result_text
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                span_id,
                trace_id,
                # seq: no convention key; use 0 as placeholder
                0,
                _attr(span, c.GEN_AI_TOOL_NAME) or "",
                _attr(span, c.GEN_AI_TOOL_CALL_ID),
                _attr(span, c.GOSLING_TOOL_ARGS),
                _attr(span, c.GOSLING_TOOL_STATUS),
                _attr(span, c.GOSLING_TOOL_ERROR_CODE),
                duration,
                raw_result,
            ),
        )
        self._conn.commit()

    def _write_fast_path_decision(self, span: ReadableSpan) -> None:
        # fast_path_decisions PK is request_id (trace_id)
        trace_id = self._trace_id(span)
        duration = _duration_ms(span.start_time, span.end_time) if span.end_time else None

        accepted_val = _attr(span, c.GOSLING_FP_ACCEPTED)

        self._conn.execute(
            """
            INSERT OR REPLACE INTO fast_path_decisions (
                request_id, backend, menu_hash, entity_id,
                confidence, threshold, accepted, skip_reason, duration_ms
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                trace_id,
                _attr(span, c.GOSLING_FP_BACKEND) or "",
                _attr(span, c.GOSLING_FP_MENU_HASH),
                _attr(span, c.GOSLING_FP_ENTITY_ID),
                _attr(span, c.GOSLING_FP_CONFIDENCE),
                _attr(span, c.GOSLING_FP_THRESHOLD),
                _bool_to_int(accepted_val),
                _attr(span, c.GOSLING_FP_SKIP_REASON),
                duration,
            ),
        )
        self._conn.commit()
