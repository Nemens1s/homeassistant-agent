"""Generate a Needle fine-tuning dataset + a held-out eval file for the local
HA agent's fast path.

This is a TEMPLATE you edit when your `automation.ai_*` menu changes. It is
dependency-free (only pyyaml) so you can run it anywhere:

    python finetune/generate_dataset.py

It writes two files next to itself:
  - train.jsonl       -> feed to `needle finetune`
  - cases.yaml        -> feed to `tests/evals/run_needle.py` (held-out eval)

How the tool representation is chosen (MUST match inference):
  The agent builds each Needle tool as
      name        = entity_id with the "automation.ai_" prefix stripped
      description = the automation's friendly name (its alias)
  because HA does not expose an automation's `description` in its state.
  So we train against exactly that. Do not invent richer descriptions here
  unless you also change how the agent builds the menu.

Lessons baked in from the spike (see docs/needle-finetuning.md):
  - ~40 phrasings per automation; a few hundred total is the sweet spot.
  - Keep tool names/aliases TOKEN-DISJOINT across automations (a 45M model
    latches onto shared words).
  - For on/off pairs, load BOTH polarities heavily and unambiguously.
  - Add reads that MENTION your devices/rooms as negatives (answers: []),
    or questions like "is the X on?" will false-fire.
  - Keep TEST phrasings distinct from TRAIN (this script asserts it).
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import yaml

OUT_DIR = Path(__file__).parent

# ---------------------------------------------------------------------------
# 1) YOUR MENU. One entry per automation.ai_* you want on the fast path.
#    entity_id -> friendly name (alias). Keep aliases token-disjoint.
# ---------------------------------------------------------------------------
MENU = {
    "automation.ai_turn_concorde_light_on": "AI: Turn Concorde Light On",
    "automation.ai_turn_concorde_light_off": "AI: Turn Concorde Light Off",
    "automation.ai_send_test_notification": "AI: Send Test Notification",
}

# ---------------------------------------------------------------------------
# 2) TRAINING phrasings. Many varied ways a user might ask for each automation.
#    Keys are entity_ids from MENU above.
# ---------------------------------------------------------------------------
TRAIN = {
    "automation.ai_turn_concorde_light_on": [
        "turn on the concorde light", "concorde on", "switch on the concorde",
        "light up the concorde", "power on the concorde plug", "put the concorde on",
        "concorde light on please", "activate the concorde light", "turn on concorde",
        # ... add ~40 total; more variety = better generalization ...
    ],
    "automation.ai_turn_concorde_light_off": [
        "turn off the concorde light", "concorde off", "switch off the concorde",
        "kill the concorde light", "power off the concorde plug", "shut off the concorde",
        "concorde light off please", "deactivate the concorde light", "turn off concorde",
        # ... add ~40 total ...
    ],
    "automation.ai_send_test_notification": [
        "send me a test notification", "ping me", "test the notifier",
        "send a test message", "fire a test alert", "send test telegram",
        "trigger a test notification", "notify me test", "test alert please",
        # ... add ~30 total ...
    ],
}

# ---------------------------------------------------------------------------
# 3) READS / off-topic. These must produce NO tool call (answers: []). INCLUDE
#    questions that mention your devices/rooms, or those will false-fire.
# ---------------------------------------------------------------------------
TRAIN_READS = [
    "is the concorde light on?", "what's the concorde plug status?",
    "is anybody home?", "what's the temperature?", "who is home right now?",
    "how much battery does my phone have?", "are the doors locked?",
    "list my automations", "what's the weather?", "is the tv on?",
    # ... add ~30 total; cover presence, climate, status, device questions ...
]

# ---------------------------------------------------------------------------
# 4) HELD-OUT TEST. DISTINCT phrasings you did NOT train on (measures
#    generalization). Same keys as MENU + a TEST_READS list.
# ---------------------------------------------------------------------------
TEST = {
    "automation.ai_turn_concorde_light_on": [
        "turn the concorde light on", "i want the concorde light on",
    ],
    "automation.ai_turn_concorde_light_off": [
        "turn the concorde light off", "i want the concorde light off",
    ],
    "automation.ai_send_test_notification": [
        "send a test ping", "test notification please",
    ],
}
TEST_READS = [
    "is the concorde light currently on?", "is anybody home right now?",
    "what's the temperature in here?",
]

AI_PREFIX = "automation.ai_"


def tool_name(entity_id: str) -> str:
    """Match app/needle/cactus_backend.py _tool_name: strip domain + ai_ prefix."""
    local = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
    if local.startswith("ai_"):
        local = local[len("ai_"):]
    local = re.sub(r"[^0-9a-zA-Z_]", "_", local)
    if not local or local[0].isdigit():
        local = "a_" + local
    return local


def build_tools() -> list[dict]:
    tools = []
    for entity_id, alias in MENU.items():
        tools.append({
            "name": tool_name(entity_id),
            "description": alias,  # = friendly name, exactly as the agent presents it
            "parameters": {"type": "object", "properties": {}},
        })
    return tools


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def main() -> None:
    tools = build_tools()
    test_norms = {_norm(x) for v in TEST.values() for x in v}
    for r in TEST_READS:
        test_norms.add(_norm(r))

    rows = []
    for entity_id, phrasings in TRAIN.items():
        name = tool_name(entity_id)
        for q in phrasings:
            assert _norm(q) not in test_norms, f"TRAIN/TEST overlap: {q!r}"
            rows.append({
                "query": q, "tools": tools,
                "answers": [{"name": name, "arguments": {}}],
                "reasoning": f"user intent matches {name}",
            })
    for q in TRAIN_READS:
        assert _norm(q) not in test_norms, f"TRAIN/TEST overlap: {q!r}"
        rows.append({"query": q, "tools": tools, "answers": [],
                     "reasoning": "a question/read, not a trigger"})

    random.seed(11)
    random.shuffle(rows)
    train_path = OUT_DIR / "train.jsonl"
    with train_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    menu = []
    for entity_id, alias in MENU.items():
        menu.append({"entity_id": entity_id, "name": alias})
    cases = []
    for entity_id, phrasings in TEST.items():
        for q in phrasings:
            cases.append({"prompt": q, "expect_tool": "trigger_automation",
                          "expect_params": {"entity_id": entity_id}})
    for q in TEST_READS:
        cases.append({"prompt": q, "expect_not_tool": "trigger_automation"})
    cases_path = OUT_DIR / "cases.yaml"
    yaml.safe_dump({"menu": menu, "cases": cases}, cases_path.open("w"), sort_keys=False)

    n_pos = sum(len(v) for v in TRAIN.values())
    print(f"wrote {train_path}  ({len(rows)} rows: {n_pos} positive, {len(TRAIN_READS)} reads)")
    print(f"wrote {cases_path}  ({len(cases)} held-out test cases)")


if __name__ == "__main__":
    main()
