"""Runs tests/evals/deterministic.yaml: the pre-fetch decision per prompt,
no LLM, no HA."""
from pathlib import Path

import pytest
import yaml

from app.agent.middleware.prefetch_middleware import decide_prefetch
from app.agent.tool_router import _KEYWORDS, CORE_TOOLS, select_tool_names

_ROWS = yaml.safe_load((Path(__file__).parent / "evals" / "deterministic.yaml").read_text())
_IDS = []
for _row in _ROWS:
    _IDS.append(_row["prompt"])

AREAS = [
    {"area_id": "bedroom", "name": "Bedroom", "aliases": []},
    {"area_id": "master_bedroom", "name": "Master Bedroom", "aliases": []},
    {"area_id": "hall", "name": "Hall", "aliases": []},
    {"area_id": "kitchen", "name": "Kitchen", "aliases": []},
    {"area_id": "living_room", "name": "Living Room", "aliases": []},
]
KNOWN = {"light.living_room_lamp", "sensor.kitchen_temperature",
         "sensor.outside_temperature", "binary_sensor.bedroom_window"}
ALL_TOOLS = set(CORE_TOOLS) | set(_KEYWORDS)


@pytest.mark.parametrize("row", _ROWS, ids=_IDS)
def test_deterministic_row(row):
    enabled = ALL_TOOLS - set(row.get("disabled") or [])
    # The menu production offers: tool subsetting (on by default) runs first,
    # so a trigger whose tool the router does not offer can never fire.
    available = select_tool_names(enabled, row["prompt"])
    got = decide_prefetch(row["prompt"], available, AREAS, KNOWN,
                          request_area=row.get("request_area"))
    expected = row["prefetch"]
    if expected is None:
        assert got is None
    else:
        assert got is not None
        assert (got.tool, got.args, got.rule) == (expected["tool"], expected["args"], expected["rule"])
