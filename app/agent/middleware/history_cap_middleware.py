from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import RemoveMessage, trim_messages


def history_removals(messages: list, max_messages: int) -> list:
    """RemoveMessage entries that trim `messages` down to the last
    `max_messages`, snapped to a human-turn boundary so a kept window never
    starts on an orphaned tool result or splits a tool-call group. Returns []
    when nothing should be pruned (cap disabled, already small, or no safe cut).
    """
    if max_messages <= 0 or len(messages) <= max_messages:
        return []
    kept = trim_messages(
        messages,
        max_tokens=max_messages,
        token_counter=len,  # count messages, not tokens
        strategy="last",
        start_on="human",
        include_system=False,
        allow_partial=False,
    )
    if not kept:
        return []  # no human boundary found within the cap — keep everything
    kept_ids = set()
    for m in kept:
        kept_ids.add(m.id)
    removals = []
    for m in messages:
        if m.id not in kept_ids:
            removals.append(RemoveMessage(id=m.id))
    return removals


class HistoryCapMiddleware(AgentMiddleware):
    """After each run, prune persisted history to the last `max_messages`
    messages. Unlike ContextWindowMiddleware — which trims only the per-call
    view and leaves the checkpoint intact — this is a DELIBERATE, durable prune
    of checkpointed state, to bound per-thread growth. Off when max_messages<=0."""

    def __init__(self, max_messages: int):
        super().__init__()
        self._max = max_messages

    def _update(self, state) -> dict | None:
        messages = state.get("messages") or []
        removals = history_removals(messages, self._max)
        if not removals:
            return None
        return {"messages": removals}

    def after_agent(self, state, runtime) -> dict | None:
        return self._update(state)

    async def aafter_agent(self, state, runtime) -> dict | None:
        return self._update(state)
