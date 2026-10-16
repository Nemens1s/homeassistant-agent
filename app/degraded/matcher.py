"""Fuzzy match of free text against action names, for when the LLM is down.

Never guesses on a write: an action fires only with one clear winner (high
score AND a clear margin over the runner-up). Anything less returns a short
candidate list for the user to pick from with /run <n>.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

FIRE_SCORE = 0.80
FIRE_MARGIN = 0.15
SUGGEST_SCORE = 0.50
MAX_CANDIDATES = 3

_PREFIX = "ai action:"
# Words that carry no meaning on their own. on/off and start/stop are NOT
# here: they are exactly what tells "Lights On" from "Lights Off".
_FILLER = frozenset({
    "please", "can", "could", "you", "the", "a", "an",
    "turn", "switch", "run", "do", "my", "to",
})
_NON_WORD = re.compile(r"[^\w\s]")


@dataclass(frozen=True)
class Match:
    winner: dict | None
    candidates: list[dict] = field(default_factory=list)


def normalise(text: str) -> list[str]:
    lowered = text.lower().strip()
    if lowered.startswith(_PREFIX):
        lowered = lowered[len(_PREFIX):]
    lowered = _NON_WORD.sub(" ", lowered)
    words = []
    for word in lowered.split():
        if word not in _FILLER:
            words.append(word)
    return words


def score(query: str, name: str) -> float:
    query_words = set(normalise(query))
    name_words = set(normalise(name))
    if not query_words or not name_words:
        return 0.0
    jaccard = len(query_words & name_words) / len(query_words | name_words)
    # Sorted, so word order ("lights on" vs "on lights") does not matter.
    query_text = " ".join(sorted(query_words))
    name_text = " ".join(sorted(name_words))
    ratio = difflib.SequenceMatcher(None, query_text, name_text).ratio()
    return 0.6 * jaccard + 0.4 * ratio


def _row_name(row: dict) -> str:
    name = row.get("name") or ""
    if name:
        return name
    return (row.get("entity_id") or "").split(".", 1)[-1].replace("_", " ")


def _by_score(pair: tuple[float, int]) -> tuple[float, int]:
    value, index = pair
    return (-value, index)  # best first; ties keep input order


def match_action(text: str, rows: list[dict]) -> Match:
    scored = []
    index = 0
    for row in rows:
        scored.append((score(text, _row_name(row)), index))
        index += 1
    if not scored:
        return Match(winner=None)
    scored.sort(key=_by_score)
    best_score, best_index = scored[0]
    second_score = 0.0
    if len(scored) > 1:
        second_score = scored[1][0]
    if best_score >= FIRE_SCORE and best_score - second_score >= FIRE_MARGIN:
        return Match(winner=rows[best_index])
    candidates = []
    for value, row_index in scored[:MAX_CANDIDATES]:
        if value >= SUGGEST_SCORE:
            candidates.append(rows[row_index])
    return Match(winner=None, candidates=candidates)
