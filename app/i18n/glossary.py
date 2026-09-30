"""Entity glossary: protects device and room names from the MT models.

Translation models mangle proper nouns,
so every known name is swapped for a placeholder before translation and put
back afterwards — as the English canonical form on the way in, as the first
native form on the way out.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("agent.i18n")

# NLLB's sentencepiece vocabulary has no ⟦ or ⟧ — they become <unk> and the
# placeholder is destroyed at tokenization. Brackets survive.
PLACEHOLDER_OPEN = "["
PLACEHOLDER_CLOSE = "]"


def placeholder(index: int) -> str:
    return f"{PLACEHOLDER_OPEN}E{index}{PLACEHOLDER_CLOSE}"


PLACEHOLDER_RE = re.compile(
    re.escape(PLACEHOLDER_OPEN) + r"E\d+" + re.escape(PLACEHOLDER_CLOSE)
)


@dataclass(slots=True)
class GlossaryEntry:
    id: str
    en: str
    forms: dict[str, list[str]] = field(default_factory=dict)

    def native(self, language: str) -> str:
        """The form to restore for *language* — the nominative, or English."""
        forms = self.forms.get(language) or []
        if forms:
            return forms[0]
        return self.en


@dataclass(slots=True)
class Protected:
    """Result of protecting a text: masked text plus what each mask stands for."""

    text: str
    entries: dict[str, GlossaryEntry] = field(default_factory=dict)
    hits: list[str] = field(default_factory=list)


class Glossary:
    def __init__(self, entries: list[GlossaryEntry]) -> None:
        self._entries = entries

    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        """Load a glossary file. A missing or broken file yields an empty one."""
        p = Path(path)
        if not p.exists():
            log.info("glossary: does not exist")
            return cls([])
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        except Exception:
            log.warning("glossary: could not parse %s — continuing without it", p, exc_info=True)
            return cls([])

        log.info("glossary: found")
        entries = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            english = item.get("en")
            if not english:
                continue
            entry_id = item.get("id") or english
            forms: dict[str, list[str]] = {"en": [english]}
            for key, value in item.items():
                if key in ("id", "en"):
                    continue
                if isinstance(value, str):
                    forms[key] = [value]
                elif isinstance(value, list):
                    forms[key] = [v for v in value if isinstance(v, str)]
            entries.append(GlossaryEntry(id=entry_id, en=english, forms=forms))
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    def protect(self, text: str, languages: list[str] | None = None) -> Protected:
        """Replace known names with placeholders, longest match first.

        *languages* limits which form lists are matched; None matches them all.
        """
        candidates = []
        for entry in self._entries:
            for language, forms in entry.forms.items():
                if languages is not None and language not in languages:
                    continue
                for form in forms:
                    if form:
                        candidates.append((form, entry))
        candidates.sort(key=lambda pair: len(pair[0]), reverse=True)

        # Claim spans longest-first so "living room" wins over "room", then
        # number the placeholders left to right so the text still reads in order.
        taken = [False] * len(text)
        spans: list[tuple[int, int, GlossaryEntry]] = []
        for form, entry in candidates:
            for match in re.finditer(re.escape(form), text, re.IGNORECASE):
                if any(taken[match.start():match.end()]):
                    continue
                for i in range(match.start(), match.end()):
                    taken[i] = True
                spans.append((match.start(), match.end(), entry))
        spans.sort(key=lambda span: span[0])

        entries: dict[str, GlossaryEntry] = {}
        hits: list[str] = []
        by_entry: dict[str, str] = {}
        pieces = []
        cursor = 0
        for begin, finish, entry in spans:
            mark = by_entry.get(entry.id)
            if mark is None:
                mark = placeholder(len(entries) + 1)
                by_entry[entry.id] = mark
                entries[mark] = entry
                hits.append(entry.id)
            pieces.append(text[cursor:begin])
            pieces.append(mark)
            cursor = finish
        pieces.append(text[cursor:])
        result = "".join(pieces)

        return Protected(text=result, entries=entries, hits=hits)

    def restore(self, text: str, protected: Protected, language: str) -> str:
        """Put the names back, in *language*. Unknown placeholders are dropped."""
        result = text
        for mark, entry in protected.entries.items():
            if language == "en":
                replacement = entry.en
            else:
                replacement = entry.native(language)
            result = result.replace(mark, replacement)
        return result
