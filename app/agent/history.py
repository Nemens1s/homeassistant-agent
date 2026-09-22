"""Reconstruct a display transcript from checkpointed LangGraph messages.

The frontend renders conversation history from the server (the checkpointer is
the source of truth), not from browser localStorage. The checkpointer stores the
full agent message list — human turns, tool-calling AIMessages, ToolMessages,
and final replies. For display we keep only what the user should see: their own
messages and the assistant's visible replies (thinking stripped, tool plumbing
dropped).
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from app.telemetry.thinking import split_thinking


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
            _, text = split_thinking(message)
            if text.strip():
                turns.append({"role": "bot", "text": text})
    return turns
