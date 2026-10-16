"""Runs registry tools for the router through the same adapter the agent
uses — the pattern EventRunner._run_direct established — so every gate,
audit row and error envelope is identical to an agent call."""

from __future__ import annotations

import json

from app.tools import registry
from app.tools.adapter import LoopGuard, to_structured_tool


class ToolRunner:
    def __init__(self, ctx, enabled: set[str] | None, thread_id: str) -> None:
        self._ctx = ctx
        self._enabled = enabled  # app.state.enabled_tools; None = tier check only
        self._thread_id = thread_id
        self.calls: list[dict] = []

    def has(self, name: str) -> bool:
        definition = registry.get(name)
        if definition is None:
            return False
        if self._enabled is not None:
            return name in self._enabled
        return definition.tier <= self._ctx.settings.max_tier

    async def run(self, name: str, args: dict) -> dict:
        # A fresh guard per call: the router never loops, and two identical
        # commands in a row must both run.
        tool = to_structured_tool(registry.get(name), self._ctx, LoopGuard())
        config = {"configurable": {"thread_id": self._thread_id}}
        raw = await tool.ainvoke(args, config=config)
        envelope = json.loads(raw)
        self.calls.append({"name": name, "status": envelope.get("status", "ok")})
        return envelope
