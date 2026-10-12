from __future__ import annotations

import time
from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware


class WatchedEntitiesMiddleware(AgentMiddleware):
    """Puts the user's watched entities (id, name, state) on the latest user turn
    for this model call only, so the model skips search round-trips. The
    checkpointed messages are never changed."""

    def __init__(self, rest, entity_ids, cap: int = 30, ttl_s: float = 20.0):
        super().__init__()
        self._rest = rest
        self._ids = list(entity_ids)
        self._cap = cap
        self._ttl_s = ttl_s
        self._cached_block = None
        self._cached_at = 0.0

    async def _block(self) -> str:
        now = time.monotonic()
        if self._cached_block is not None and now - self._cached_at < self._ttl_s:
            return self._cached_block
        lines = ["<home_context>",
                 "Watched devices (use these entity_ids directly; no need to search):"]
        for eid in self._ids:
            try:
                s = await self._rest.get_state(eid)
            except Exception:
                lines.append(f"- {eid}")
                continue
            name = s.get("attributes", {}).get("friendly_name", "")
            lines.append(f'- {eid} "{name}" = {s.get("state", "")}')
        lines.append("</home_context>")
        self._cached_block = "\n".join(lines)
        self._cached_at = now
        return self._cached_block

    async def awrap_model_call(self, request, handler):
        if not self._ids or len(self._ids) > self._cap:
            return await handler(request)
        index = None
        for i in range(len(request.messages) - 1, -1, -1):
            if getattr(request.messages[i], "type", None) == "human":
                index = i
                break
        if index is None or not isinstance(request.messages[index].content, str):
            return await handler(request)
        block = await self._block()
        messages = list(request.messages)
        original = messages[index]
        messages[index] = original.model_copy(update={"content": original.content + "\n\n" + block})
        try:
            prepared = replace(request, messages=messages)
        except TypeError:  # pragma: no cover
            prepared = request.override(messages=messages)
        return await handler(prepared)

    def wrap_model_call(self, request, handler):
        return handler(request)
