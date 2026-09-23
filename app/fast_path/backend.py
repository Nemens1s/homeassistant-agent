"""Backend-agnostic fast-path inference seam. Any classifier that maps an
utterance + menu to a Decision plugs in here; the middleware depends only on
this protocol, never on a concrete backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.needle.menu import Menu


@dataclass(frozen=True)
class Decision:
    entity_id: str | None   # constrained to the current menu, or None
    confidence: float
    arguments: dict = field(default_factory=dict)


class FastPathBackend(Protocol):
    name: str
    async def classify(self, message: str, menu: Menu) -> Decision: ...


class FakeBackend:
    """Test backend: per-message Decision if provided, else a scripted Decision,
    else a no-op (None, 0.0)."""

    name = "fake"

    def __init__(self, decision: Decision | None = None, by_message: dict | None = None):
        self._decision = decision
        self._by_message = by_message or {}

    async def classify(self, message: str, menu: Menu) -> Decision:
        if message in self._by_message:
            return self._by_message[message]
        if self._decision is not None:
            return self._decision
        return Decision(entity_id=None, confidence=0.0)
