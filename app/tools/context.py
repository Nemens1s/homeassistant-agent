"""Dependencies handed to every tool handler. Handlers never import clients
directly — tests pass a fake context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.ha.websocket import ws_is_ready

_DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@dataclass
class ToolContext:
    settings: Settings
    rest: Any  # RestClient; typed loosely so tests can pass fakes
    ws: Any = None  # WebSocketClient | None
    skills_dir: Path = field(default=_DEFAULT_SKILLS_DIR)
    audit: Any = None


def ws_ready(ctx: "ToolContext") -> bool:
    """Whether the websocket tools can run now (see ws_is_ready)."""
    return ws_is_ready(ctx.ws)
