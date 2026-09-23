"""Translates the agent's message stream into the SSE event protocol shared
by the endpoint and the frontend. The CLI keeps its simpler inline filter —
two consumers with different presentations; revisit only if a third appears."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError



class _ThinkBuffer:
    """Splits a streaming text into thinking and content chunks.

    Handles <think>...</think> tags that may be split across chunk boundaries,
    as produced by llama.cpp and other OpenAI-compatible providers that inline
    reasoning in the content stream. Ollama exposes reasoning separately via
    additional_kwargs["reasoning_content"] and produces no <think> tags, so
    feeding Ollama content through this buffer is a safe no-op.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, text: str) -> list[dict]:
        self._buf += text
        events: list[dict] = []
        while self._buf:
            if self._in_think:
                end = self._buf.find(self._CLOSE)
                if end == -1:
                    hold = self._partial_suffix(self._buf, self._CLOSE)
                    flush, self._buf = self._buf[: len(self._buf) - hold], self._buf[len(self._buf) - hold :]
                    if flush:
                        events.append({"type": "thinking", "text": flush})
                    break
                if end > 0:
                    events.append({"type": "thinking", "text": self._buf[:end]})
                self._buf = self._buf[end + len(self._CLOSE) :]
                self._in_think = False
            else:
                start = self._buf.find(self._OPEN)
                if start == -1:
                    hold = self._partial_suffix(self._buf, self._OPEN)
                    flush, self._buf = self._buf[: len(self._buf) - hold], self._buf[len(self._buf) - hold :]
                    if flush:
                        events.append({"type": "token", "text": flush})
                    break
                if start > 0:
                    events.append({"type": "token", "text": self._buf[:start]})
                self._buf = self._buf[start + len(self._OPEN) :]
                self._in_think = True
        return events

    def flush(self) -> list[dict]:
        if not self._buf:
            return []
        kind = "thinking" if self._in_think else "token"
        event = {"type": kind, "text": self._buf}
        self._buf = ""
        return [event]

    @staticmethod
    def _partial_suffix(buf: str, tag: str) -> int:
        """Length of a partial tag prefix that ends `buf`, so we don't emit it yet."""
        for n in range(min(len(tag) - 1, len(buf)), 0, -1):
            if buf.endswith(tag[:n]):
                return n
        return 0


async def stream_events(
    agent, message: str, thread_id: str, recursion_limit: int
) -> AsyncIterator[dict]:
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    think_buf = _ThinkBuffer()
    parts: list[str] = []
    try:
        async for msg, _meta in agent.astream(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
            stream_mode="messages",
        ):
            if isinstance(msg, AIMessage):
                for tc in msg.tool_calls:
                    yield {"type": "tool_call", "name": tc["name"], "args": tc["args"]}

                # Ollama reasoning models expose thinking here; other providers leave it empty.
                thinking = msg.additional_kwargs.get("reasoning_content", "")
                if thinking:
                    yield {"type": "thinking", "text": thinking}

                # For llama.cpp and similar, reasoning is inlined as <think>…</think> in content.
                if isinstance(msg.content, str) and msg.content:
                    for event in think_buf.feed(msg.content):
                        if event["type"] == "token":
                            parts.append(event["text"])
                        yield event

            elif isinstance(msg, ToolMessage):
                try:
                    status = json.loads(msg.content).get("status", "ok")
                except (TypeError, ValueError):
                    status = "ok"
                yield {"type": "tool_result", "name": msg.name or "", "status": status}

    except GraphRecursionError:
        yield {
            "type": "error",
            "message": (
                f"Stopped after {recursion_limit} steps without reaching an answer. "
                "Try a more specific question."
            ),
        }
        return

    for event in think_buf.flush():
        if event["type"] == "token":
            parts.append(event["text"])
        yield event

    yield {"type": "done", "reply": "".join(parts)}
