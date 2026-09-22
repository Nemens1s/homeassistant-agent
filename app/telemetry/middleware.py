"""TelemetryMiddleware: emits OTel spans for each model step and tool call.

One ``chat <model>`` span per model step; one ``execute_tool <name>`` span per
tool call.  Every individual attribute read is wrapped in ``_safe_set`` so that
a telemetry exception never changes the handler result (fail-open guarantee).
"""
from __future__ import annotations

import contextvars
import json
import logging

from langchain.agents.middleware import AgentMiddleware

from app.telemetry import conventions as C
from app.telemetry.thinking import split_thinking

_log = logging.getLogger("agent.telemetry")

# 1-based step counter set by the agent loop (or left at default 0 when not set).
current_step: contextvars.ContextVar[int] = contextvars.ContextVar(
    "gosling_step", default=0
)

_MAX_RESULT_BYTES = 64 * 1024  # 64 KB cap for GOSLING_TOOL_RESULT


def _safe_set(span, key: str, fn) -> None:
    """Call fn(), set span attribute key to the result.

    Swallows any exception so a telemetry failure never propagates to the caller.
    """
    try:
        value = fn()
        if value is not None:
            span.set_attribute(key, value)
    except Exception:
        _log.debug("telemetry _safe_set failed for key=%r", key, exc_info=True)


def _extract_ai_message(response):
    """Return the first AIMessage from a ModelResponse or the raw AIMessage."""
    from langchain.agents.middleware import ModelResponse
    from langchain_core.messages import AIMessage
    if isinstance(response, ModelResponse):
        for msg in response.result:
            if isinstance(msg, AIMessage):
                return msg
        return None
    if isinstance(response, AIMessage):
        return response
    return None


class TelemetryMiddleware(AgentMiddleware):
    """Emits ``chat <model>`` and ``execute_tool <name>`` OTel spans."""

    def __init__(self, tracer) -> None:
        super().__init__()
        self._tracer = tracer

    # ------------------------------------------------------------------
    # Model call
    # ------------------------------------------------------------------

    async def awrap_model_call(self, request, handler):
        span_name = "chat"
        with self._tracer.start_as_current_span(span_name) as span:
            # --- pre-call attributes ---
            _safe_set(span, C.GOSLING_TOOLS_OFFERED, lambda: json.dumps(
                [t.name for t in request.tools]
            ))
            _safe_set(span, C.GOSLING_MESSAGES_COUNT, lambda: len(request.messages))
            _safe_set(span, C.GOSLING_STEP, lambda: current_step.get())

            result = await handler(request)

            # --- post-call attributes ---
            ai_msg = _extract_ai_message(result)
            if ai_msg is not None:
                _safe_set(span, C.GEN_AI_RESPONSE_MODEL, lambda: ai_msg.response_metadata.get("model_name"))
                _safe_set(span, C.GOSLING_FAST_PATH, lambda: bool(ai_msg.response_metadata.get("fast_path", False)))
                _safe_set_thinking_content(span, ai_msg)
                _safe_set(span, C.GEN_AI_USAGE_INPUT_TOKENS, lambda: (
                    ai_msg.usage_metadata.get("input_tokens")
                    if ai_msg.usage_metadata else None
                ))
                _safe_set(span, C.GEN_AI_USAGE_OUTPUT_TOKENS, lambda: (
                    ai_msg.usage_metadata.get("output_tokens")
                    if ai_msg.usage_metadata else None
                ))
                _safe_set(span, C.GOSLING_TOOL_CALLS, lambda: json.dumps([
                    {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")}
                    for tc in (ai_msg.tool_calls or [])
                ]) if ai_msg.tool_calls else None)

        return result

    def wrap_model_call(self, request, handler):
        """Sync mirror of awrap_model_call for CLI usage."""
        span_name = "chat"
        with self._tracer.start_as_current_span(span_name) as span:
            _safe_set(span, C.GOSLING_TOOLS_OFFERED, lambda: json.dumps(
                [t.name for t in request.tools]
            ))
            _safe_set(span, C.GOSLING_MESSAGES_COUNT, lambda: len(request.messages))
            _safe_set(span, C.GOSLING_STEP, lambda: current_step.get())

            result = handler(request)

            ai_msg = _extract_ai_message(result)
            if ai_msg is not None:
                _safe_set(span, C.GEN_AI_RESPONSE_MODEL, lambda: ai_msg.response_metadata.get("model_name"))
                _safe_set(span, C.GOSLING_FAST_PATH, lambda: bool(ai_msg.response_metadata.get("fast_path", False)))
                _safe_set_thinking_content(span, ai_msg)
                _safe_set(span, C.GEN_AI_USAGE_INPUT_TOKENS, lambda: (
                    ai_msg.usage_metadata.get("input_tokens")
                    if ai_msg.usage_metadata else None
                ))
                _safe_set(span, C.GEN_AI_USAGE_OUTPUT_TOKENS, lambda: (
                    ai_msg.usage_metadata.get("output_tokens")
                    if ai_msg.usage_metadata else None
                ))

        return result

    # ------------------------------------------------------------------
    # Tool call
    # ------------------------------------------------------------------

    async def awrap_tool_call(self, request, handler):
        tool_name = ""
        try:
            tool_name = request.tool_call["name"]
        except Exception:
            pass

        span_name = f"execute_tool {tool_name}" if tool_name else "execute_tool"
        with self._tracer.start_as_current_span(span_name) as span:
            # --- pre-call attributes ---
            _safe_set(span, C.GEN_AI_TOOL_NAME, lambda: request.tool_call["name"])
            _safe_set(span, C.GEN_AI_TOOL_CALL_ID, lambda: request.tool_call["id"])
            _safe_set(span, C.GOSLING_TOOL_ARGS, lambda: json.dumps(
                request.tool_call["args"], sort_keys=True
            ))

            result = await handler(request)

            # --- post-call attributes from JSON envelope ---
            _safe_set(span, C.GOSLING_TOOL_RESULT, lambda: _cap_content(result.content))
            _safe_set(span, C.GOSLING_TOOL_STATUS, lambda: _parse_status(result.content))
            _safe_set(span, C.GOSLING_TOOL_ERROR_CODE, lambda: _parse_error_code(result.content))

        return result


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _safe_set_thinking_content(span, ai_msg) -> None:
    """Split thinking/content and set span attributes; swallows any exception."""
    try:
        thinking, content = split_thinking(ai_msg)
        if thinking:
            span.set_attribute(C.GOSLING_THINKING_TEXT, thinking)
        if content:
            span.set_attribute(C.GOSLING_CONTENT_TEXT, content)
    except Exception:
        _log.debug("telemetry _safe_set_thinking_content failed", exc_info=True)


def _cap_content(content: str) -> str:
    """Cap content string to _MAX_RESULT_BYTES (UTF-8 bytes)."""
    encoded = content.encode("utf-8")
    if len(encoded) <= _MAX_RESULT_BYTES:
        return content
    return encoded[:_MAX_RESULT_BYTES].decode("utf-8", errors="replace")


def _parse_status(content: str) -> str | None:
    try:
        data = json.loads(content)
        return data.get("status")
    except Exception:
        return None


def _parse_error_code(content: str) -> str | None:
    try:
        data = json.loads(content)
        error = data.get("error")
        if isinstance(error, dict):
            return error.get("code")
        return None
    except Exception:
        return None
