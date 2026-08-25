"""Needle fast-path eval — measures confident-fraction x accuracy on the
trigger-automation subset, independent of Ollama/HA.

    python -m tests.evals.run_needle
    python -m tests.evals.run_needle --threshold 0.7 --cases tests/evals/cases-needle.yaml

Needs `cactus-needle` installed. NOT collected by pytest. Confidence/accuracy are
model properties (CPU-independent), so this is valid to run on any machine; only
latency would differ from the deployment host.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import yaml

from app.needle.cactus_backend import CactusBackend
from app.needle.menu import Menu, MenuItem
from tests.evals.run import check  # reuse the exact scoring used by the main eval

CASES_FILE = Path(__file__).parent / "cases-needle.yaml"


def _load(path: Path):
    data = yaml.safe_load(path.read_text())
    items = []
    for m in data["menu"]:
        items.append(MenuItem(m["entity_id"], m.get("name", m["entity_id"]),
                              m.get("description", "")))
    menu = Menu(items=tuple(items), signature="eval")
    return menu, data["cases"]


def _to_tool_calls(decision, threshold):
    if decision.entity_id is not None and decision.confidence >= threshold:
        return [{"name": "trigger_automation", "args": {"entity_id": decision.entity_id}}]
    return []


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(CASES_FILE))
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--model-path", default="")
    args = parser.parse_args()

    menu, cases = _load(Path(args.cases))
    backend = CactusBackend(model_path=args.model_path)

    passed = 0
    fired = 0
    for case in cases:
        decision = await backend.classify(case["prompt"], menu)
        did_fire = decision.entity_id is not None and decision.confidence >= args.threshold
        if did_fire:
            fired += 1
        ok, detail = check(case, _to_tool_calls(decision, args.threshold))
        if ok:
            passed += 1
        mark = "PASS" if ok else "FAIL"
        chosen = decision.entity_id or "(none)"
        note = f"  {detail}" if detail else ""
        print(f"[{mark}] {case['prompt']!r:38} conf={decision.confidence:.3f} -> {chosen}{note}")

    total = len(cases)
    print(f"\naccuracy: {passed}/{total}    fast-path fired: {fired}/{total} "
          f"(threshold {args.threshold})")


if __name__ == "__main__":
    asyncio.run(main())
