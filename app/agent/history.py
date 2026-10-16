"""Reconstruct a display transcript from checkpointed LangGraph messages.

The frontend renders conversation history from the server (the checkpointer is
the source of truth), not from browser localStorage. The checkpointer stores the
full agent message list — human turns, tool-calling AIMessages, ToolMessages,
and final replies. For display we keep only what the user should see: their own
messages and the assistant's visible replies (thinking stripped, tool plumbing
dropped).
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage

from app.telemetry.thinking import split_thinking

log = logging.getLogger("agent.history")

# Closes a turn whose model call failed because the LLM was unreachable, so
# the thread never holds two user messages in a row. Hidden from the UI.
UNANSWERED_MARKER = "(not answered: language model unavailable)"


async def close_dangling_turn(agent, thread_id: str) -> None:
    """If the thread ends with a user message (its model call failed),
    append the marker as the model's reply. Best-effort; never raises."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await agent.aget_state(config)
        messages = (snapshot.values or {}).get("messages", [])
        if messages and isinstance(messages[-1], HumanMessage):
            await agent.aupdate_state(
                config, {"messages": [AIMessage(content=UNANSWERED_MARKER)]}, as_node="model",
            )
    except Exception:
        log.warning("could not close dangling turn for thread %s", thread_id, exc_info=True)


def display_transcript(messages) -> list[dict]:
    """Map checkpointed messages to [{role, text}, ...] for the UI.

    - HumanMessage → a "user" turn.
    - AIMessage with visible content → a "bot" turn (thinking removed).
    - Tool-call-only AIMessages and ToolMessages carry no visible text and are
      skipped.
    """
    turns = []
    for message in messages or []:
        if isinstance(message, HumanMessage):
            _, text = split_thinking(message)
            if text.strip():
                turns.append({"role": "user", "text": text})
        elif isinstance(message, AIMessage):
            if message.content == UNANSWERED_MARKER:
                continue
            _, text = split_thinking(message)
            if text.strip():
                turns.append({"role": "bot", "text": text})
    return turns
