"""Runs tests/evals/deterministic.yaml: the pre-fetch decision per prompt,
no LLM, no HA."""
from pathlib import Path

import pytest
import yaml

from app.agent.middleware.prefetch_middleware import decide_prefetch
from app.agent.tool_router import _KEYWORDS, CORE_TOOLS

_ROWS = yaml.safe_load((Path(__file__).parent / "evals" / "deterministic.yaml").read_text())

AREAS = [
    {"area_id": "bedroom", "name": "Bedroom", "aliases": []},
    {"area_id": "kitchen", "name": "Kitchen", "aliases": []},
    {"area_id": "living_room", "name": "Living Room", "aliases": []},
]
KNOWN = {"light.living_room_lamp", "sensor.kitchen_temperature"}
ALL_TOOLS = set(CORE_TOOLS) | set(_KEYWORDS)


@pytest.mark.parametrize("row", _ROWS, ids=[r["prompt"] for r in _ROWS])
def test_deterministic_row(row):
    available = ALL_TOOLS - set(row.get("disabled") or [])
    got = decide_prefetch(row["prompt"], available, AREAS, KNOWN)
    expected = row["prefetch"]
    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert (got.tool, got.args, got.rule) == (expected["tool"], expected["args"], expected["rule"])
