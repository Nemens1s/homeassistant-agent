"""Translates the agent's message stream into the SSE event protocol shared
by the endpoint and the frontend. The CLI keeps its simpler inline filter —
two consumers with different presentations; revisit only if a third appears."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError


async def stream_events(
    agent, message: str, thread_id: str, recursion_limit: int
) -> AsyncIterator[dict]:
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    parts: list[str] = []
    try:
        async for msg, _meta in agent.astream(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
            stream_mode="messages",
        ):
            if isinstance(msg, AIMessageChunk):
                for tc in msg.tool_calls:
                    yield {"type": "tool_call", "name": tc["name"], "args": tc["args"]}
                if isinstance(msg.content, str) and msg.content:
                    parts.append(msg.content)
                    yield {"type": "token", "text": msg.content}
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
    yield {"type": "done", "reply": "".join(parts)}
