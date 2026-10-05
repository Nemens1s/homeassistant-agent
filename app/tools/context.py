"""Dependencies handed to every tool handler. Handlers never import clients
directly — tests pass a fake context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.ha.websocket import ws_is_ready
from app.i18n.adapter import NoopLanguageAdapter

_DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@dataclass
class ToolContext:
    settings: Settings
    rest: Any  # RestClient; typed loosely so tests can pass fakes
    ws: Any = None  # WebSocketClient | None
    skills_dir: Path = field(default=_DEFAULT_SKILLS_DIR)
    audit: Any = None
    notes: Any = None  # NoteStore | None — None when the note store is unavailable
    lang: Any = field(default_factory=NoopLanguageAdapter)  # fail-open translation for harness text


def ws_ready(ctx: "ToolContext") -> bool:
    """Whether the websocket tools can run now (see ws_is_ready)."""
    return ws_is_ready(ctx.ws)
