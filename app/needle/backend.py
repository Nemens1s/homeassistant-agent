"""Swappable inference backend. The router depends only on the NeedleBackend
protocol; CactusBackend (in-process) is added in a later task. FakeBackend
drives all router unit tests without a real runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.needle.menu import Menu


@dataclass(frozen=True)
class Decision:
    entity_id: str | None   # grammar-constrained to the current menu, or None
    confidence: float


class NeedleBackend(Protocol):
    async def classify(self, message: str, menu: Menu) -> Decision: ...


class FakeBackend:
    """Test backend: returns a per-message Decision if provided, else a single
    scripted Decision, else a no-op (None, 0.0)."""

    def __init__(self, decision: Decision | None = None, by_message: dict | None = None):
        self._decision = decision
        self._by_message = by_message or {}

    async def classify(self, message: str, menu: Menu) -> Decision:
        if message in self._by_message:
            return self._by_message[message]
        if self._decision is not None:
            return self._decision
        return Decision(entity_id=None, confidence=0.0)
