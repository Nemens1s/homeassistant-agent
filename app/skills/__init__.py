"""Skills: markdown playbooks with progressive disclosure. The system prompt
lists name+description one-liners; the model calls load_skill(name) to pull
the full text in only when relevant. Skills are data — iterating on playbooks
requires no code changes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class SkillMeta:
    name: str
    description: str


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.split("\n")
    try:
        close = lines.index("---", 1)  # first closing fence after line 0
    except ValueError:
        return {}, text.strip()
    meta = yaml.safe_load("\n".join(lines[1:close])) or {}
    body = "\n".join(lines[close + 1:]).strip()
    return meta, body


def list_skills(skills_dir: Path) -> list[SkillMeta]:
    metas = []
    for path in sorted(skills_dir.glob("*.md")):
        meta, _ = _parse_frontmatter(path.read_text())
        metas.append(
            SkillMeta(
                name=meta.get("name", path.stem),
                description=meta.get("description", ""),
            )
        )
    return metas


def read_skill(skills_dir: Path, name: str) -> str | None:
    for path in sorted(skills_dir.glob("*.md")):
        meta, body = _parse_frontmatter(path.read_text())
        if meta.get("name", path.stem) == name:
            return body
    return None
