"""Degraded mode: deterministic replies that need no LLM.

The CommandRouter (router.py) answers slash commands at any time and free
text while the LLM is down. It runs the ordinary tools through the adapter, so
the AI-actions gate, audit log and loop guard apply exactly as for the agent.
Replies are English and are never written to the conversation thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Reply:
    text: str
    kind: str = "command"  # "command" (slash command) | "degraded" (LLM down)
    # [{"name": tool_name, "status": "ok" | "error"}] — for the UI's activity lines.
    tool_calls: list[dict] = field(default_factory=list)
