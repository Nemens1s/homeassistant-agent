"""Skills: markdown playbooks with progressive disclosure. The system prompt
lists name+description one-liners; the model calls load_skill(name) to pull
the full text in only when relevant. Skills are data — iterating on playbooks
requires no code changes.

Skills are user-supplied (dropped into settings.skills_path), so a file
without valid frontmatter is treated as not a skill at all rather than
silently exposed under its filename — see _parse_frontmatter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class SkillMeta:
    name: str
    description: str


def _parse_frontmatter(text: str) -> tuple[dict, str] | None:
    """Return (meta, body), or None when `text` has no valid frontmatter
    block — callers skip such a file rather than fall back to its filename."""
    if not text.startswith("---"):
        return None
    lines = text.split("\n")
    try:
        close = next(i for i, l in enumerate(lines[1:], 1) if l.strip() == "---")
    except StopIteration:
        return None
    meta = yaml.safe_load("\n".join(lines[1:close])) or {}
    body = "\n".join(lines[close + 1:]).strip()
    return meta, body


def list_skills(skills_dir: Path) -> list[SkillMeta]:
    metas = []
    for path in sorted(skills_dir.glob("*.md")):
        parsed = _parse_frontmatter(path.read_text())
        if parsed is None:
            continue
        meta, _ = parsed
        metas.append(
            SkillMeta(
                name=meta.get("name", path.stem),
                description=meta.get("description", ""),
            )
        )
    return metas


def read_skill(skills_dir: Path, name: str) -> str | None:
    for path in sorted(skills_dir.glob("*.md")):
        parsed = _parse_frontmatter(path.read_text())
        if parsed is None:
            continue
        meta, body = parsed
        if meta.get("name", path.stem) == name:
            return body
    return None
