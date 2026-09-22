"""Non-streaming counterpart of streaming._ThinkBuffer: split a complete message
into (thinking, visible content)."""
from __future__ import annotations

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "thinking":
                    continue
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def split_thinking(message) -> tuple[str, str]:
    reasoning = message.additional_kwargs.get("reasoning_content", "")
    content = _flatten(message.content)
    if reasoning:
        return reasoning, content
    open_at = content.find(THINK_OPEN)
    close_at = content.find(THINK_CLOSE)
    if open_at != -1 and close_at != -1 and close_at > open_at:
        thinking = content[open_at + len(THINK_OPEN):close_at]
        visible = content[:open_at] + content[close_at + len(THINK_CLOSE):]
        return thinking, visible
    return "", content
