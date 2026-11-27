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

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.telemetry.thinking import split_thinking

log = logging.getLogger("agent.history")

# Closes a turn whose model call failed because the LLM was unreachable, so
# the thread never holds two user messages in a row. Hidden from the UI.
UNANSWERED_MARKER = "(not answered: language model unavailable)"

# Closes a turn the agent stopped at its step limit (GraphRecursionError).
# Hidden from the UI like UNANSWERED_MARKER.
STEP_LIMIT_MARKER = "(not answered: stopped at the step limit)"
_HIDDEN_MARKERS = (UNANSWERED_MARKER, STEP_LIMIT_MARKER)


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


async def close_stopped_turn(agent, thread_id: str) -> None:
    """After a recursion stop, leave the thread on a final AI reply: answer
    each unanswered tool call with an `aborted` envelope (a dangling tool call
    is rejected by some chat templates on the next turn), then append the
    marker. Best-effort; never raises."""
    from app.tools.base import ToolResult

    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await agent.aget_state(config)
        messages = (snapshot.values or {}).get("messages", [])
        if not messages:
            return
        last = messages[-1]
        if isinstance(last, AIMessage) and not last.tool_calls:
            return  # the turn already ended on a reply
        closing = []
        if isinstance(last, AIMessage):
            envelope = ToolResult.error("aborted", "stopped: step limit").to_json()
            for call in last.tool_calls:
                closing.append(ToolMessage(
                    content=envelope, tool_call_id=call["id"], name=call["name"],
                ))
        closing.append(AIMessage(content=STEP_LIMIT_MARKER))
        await agent.aupdate_state(config, {"messages": closing}, as_node="model")
    except Exception:
        log.warning("could not close stopped turn for thread %s", thread_id, exc_info=True)


def _is_action_tool(name: str | None) -> bool:
    from app.tools import registry
    from app.tools.base import Tier

    definition = registry.get(name or "")
    return definition is not None and definition.tier >= Tier.ACTION


async def ran_action_this_turn(agent, thread_id: str) -> bool:
    """True when an ACTION-tier tool (trigger_action, notify_user) already
    ran since the thread's last user message — so the router must not run
    the same text again after the LLM died mid-turn. Best-effort; never
    raises, False on any error."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await agent.aget_state(config)
        messages = (snapshot.values or {}).get("messages", [])
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                return False
            if isinstance(message, ToolMessage) and _is_action_tool(message.name):
                return True
    except Exception:
        log.warning("could not inspect the current turn of thread %s", thread_id, exc_info=True)
    return False


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
            if message.content in _HIDDEN_MARKERS:
                continue
            _, text = split_thinking(message)
            if text.strip():
                turns.append({"role": "bot", "text": text})
    return turns
