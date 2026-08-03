"""Core tool abstractions: tiers, result envelope, declarative definition.

Every tool is a ToolDefinition: a pydantic params model (validation + JSON
schema for the LLM from one source) and an async handler returning a
ToolResult. Handlers never see unvalidated input and never raise into the
agent loop — the adapter (tools/adapter.py) enforces both.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class Tier(enum.IntEnum):
    READ = 1
    ACTION = 2


@dataclass(slots=True)
class ToolResult:
    status: str  # "ok" | "error"
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def ok(cls, data: Any) -> "ToolResult":
        return cls(status="ok", data=data)

    @classmethod
    def error(cls, code: str, message: str, data: Any = None) -> "ToolResult":
        return cls(status="error", data=data, error_code=code, error_message=message)

    def to_json(self) -> str:
        out: dict[str, Any] = {"status": self.status}
        if self.error_code is not None:
            out["error"] = {"code": self.error_code, "message": self.error_message or ""}
        if self.data is not None:
            out["data"] = self.data
        return json.dumps(out, separators=(",", ":"), default=str)


@dataclass(slots=True)
class ToolDefinition:
    """One tool. name: snake_case id the LLM calls. description: what the
    LLM sees (keep under ~400 chars). handler: async (params, ctx) -> ToolResult."""

    name: str
    description: str
    params_model: type[BaseModel]
    tier: Tier
    handler: Callable[[BaseModel, Any], Awaitable[ToolResult]]


def entity_domain(entity_id: str) -> str:
    """Return the domain portion of an entity_id ('light.kitchen' → 'light')."""
    return entity_id.split(".", 1)[0]


def bound_rows(
    rows: list, *, max_rows: int, hint: str = "Narrow with filters to see the rest."
) -> dict[str, Any]:
    """Cap a result list while always reporting the true total, so the model
    knows results were cut and to narrow — instead of silently missing data."""
    shown = rows[:max_rows]
    env: dict[str, Any] = {"rows": shown, "total": len(rows)}
    if len(rows) > max_rows:
        env["truncated"] = True
        env["hint"] = hint
    return env
