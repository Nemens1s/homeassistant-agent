# Fine-tuning Needle for the fast path

How to fine-tune the [Needle 2](https://huggingface.co/Cactus-Compute/needle2)
tool-calling model on your own `automation.ai_*` menu, so the local fast path
routes real commands reliably. Re-run this whenever your AI-controllable
automations change.

**Why fine-tune at all:** zero-shot, the 45M base routes only ~2/9 held-out home
commands and is brutally brittle. After fine-tuning on ~140 examples it hit
**14/17 (82%)** on a held-out set for the real 3-automation menu, including the
hard on-vs-off distinction. See `docs/superpowers/plans/needle-spike-notes.md`
for the full spike record.

## The one thing that changes the design: no confidence for tuned weights

Needle's confidence head is calibrated for the base model and **fine-tuning
disables it** — `Needle(weights=<tuned.cact>)` reports `confidence = None`
(documented upstream in `doc/finetuning.md`). So the fast path can NOT gate on a
confidence threshold. Instead it uses **trust-the-call**:

- Model emits a tool call  → fire that automation.
- Model emits no call      → fall through to the full agent.

Reads must therefore be trained to emit **no call** (`answers: []`). (Why the
confidence can't just be re-enabled — and the real options if you ever need it —
is documented in `docs/needle-confidence.md`.) Safety is bounded structurally
regardless: the pick is grammar-constrained to your
`automation.ai_*` menu, every trigger passes the adapter's `ai_actions_switch`
gate, and each automation also self-gates on `input_boolean.ai_triggered_actions`.
The residual risk is a *mis-pick* firing the wrong one of your own automations —
minimise it with good training data (below).

## Prerequisites

- **A glibc (Debian/Ubuntu) x86-64 or Apple-Silicon machine.** `cactus-needle`
  pulls `jaxlib`, which has **no musl wheels** — it will NOT install on Alpine.
  Training on Apple Silicon (`[metal]`) is fast; CPU works but is slow.
- Python 3.12+ and: `pip install cactus-needle` (add `[metal]` on Apple Silicon,
  `[gpu]` on NVIDIA).
- This repo checked out (for `finetune/generate_dataset.py` and
  `tests/evals/run_needle.py`).

## Step 1 — list your AI menu

Get the current AI-controllable automations (entity_id + friendly name). Either
from the agent (`get_automations` → entries with `ai_controllable: true`) or from
HA Developer Tools → States, filtering `automation.ai_*`. You need, per
automation: its `entity_id` and its friendly name (alias).

## Step 2 — generate the dataset

Edit `finetune/generate_dataset.py`:

1. `MENU`: one line per `automation.ai_*` → its friendly name.
2. `TRAIN`: ~40 varied phrasings per automation. **Rules that matter (learned the
   hard way):**
   - Keep aliases/names **token-disjoint** across automations — a shared word
     (e.g. "night") makes the tiny model mis-route.
   - For on/off (or any polar pair), load **both** directions heavily and
     unambiguously; it's the hardest case.
3. `TRAIN_READS`: ~30 questions/reads → **include ones that name your devices or
   rooms** (e.g. "is the concorde on?"), or those will false-fire.
4. `TEST` / `TEST_READS`: a handful of **distinct** phrasings you did NOT train
   on. The script asserts there's no train/test overlap.

Then:

```bash
python finetune/generate_dataset.py
# -> finetune/train.jsonl  and  finetune/cases.yaml
```

The generator builds each tool exactly as the agent does at inference:
`name` = entity_id minus the `automation.ai_` prefix, `description` = the alias.
Don't diverge from that.

## Step 3 — fine-tune (LoRA)

```bash
cd finetune
needle finetune train.jsonl --epochs 20 --val-split 0.12 --out checkpoints/needle_lora.pkl
```

- The base checkpoint (`needle2.pkl`) auto-downloads to `checkpoints/` on first run.
- Watch the log: **val loss should trend down** (a healthy run reached ~1.6;
  a too-small dataset barely moved and gave a useless model).
- 10–30 epochs is the documented range for small datasets.

## Step 4 — build the deployable `.cact`

```bash
needle build checkpoints/needle2.pkl --lora checkpoints/needle_lora.pkl --out needle_home.cact --bits 4
```

- **Use `--bits 4`, not `--bits 2`.** 2-bit measurably hurt accuracy in testing
  (7/9 → 5/9); 4-bit is ~23 MB and worth it.

## Step 5 — evaluate (trust-the-call)

```bash
# from the repo root
python -m tests.evals.run_needle --threshold 0.0 --cases finetune/cases.yaml --model-path finetune/needle_home.cact
```

- **`--threshold 0.0` is required** for tuned weights: confidence is `None`
  (→ 0.0), so 0.0 means "fire whenever a call is emitted" = trust-the-call.
- Read the summary: `accuracy` (correct routing incl. reads abstaining) and
  `fast-path fired`. Aim for high accuracy AND zero read false-fires.
- If a class is weak or a read false-fires: add more phrasings / more
  device-mentioning read negatives for that case in Step 2 and retrain. This is
  the main tuning loop.

## Step 6 — deploy

1. Put `needle_home.cact` where the add-on can read it and point
   `needle_model_path` at it.
2. Set `needle_enabled: true` (and `max_tier: 2`) in the add-on options.
3. The add-on base image must be **Debian/glibc** (see Prerequisites) for the
   runtime to install.

## Gotchas cheat-sheet

| Symptom | Cause / fix |
|---------|-------------|
| `confidence` always None/0 | Expected for tuned weights. Use trust-the-call (`--threshold 0.0`). |
| `jaxlib` won't install | Alpine/musl. Use a Debian/glibc base. |
| numpy `X86_V2` crash / empty `avx` | Proxmox VM masking the CPU — set VM CPU **Type = host**. |
| Two similar tools confused | Token overlap in names/aliases; make them disjoint. |
| on/off flipped | Add more polarized on/off phrasings. |
| A question fired an automation | Add that question (and similar device-mentioning reads) as `answers: []`. |
| Model barely learned (flat val loss) | Too few examples — aim for a few hundred; raise epochs. |
| Slow inference (~2–4 s) | Sandy-Bridge/no-AVX2 host doing scalar XLA. Fine functionally; faster on AVX2/ARM hardware. |

## When to re-run

Re-run Steps 2–6 whenever you **add, remove, or rename** an `automation.ai_*`
(the model's tool vocabulary changed). The generator + eval are the repeatable
core; keep your phrasing pools in `generate_dataset.py` under version control so
each retrain starts from your curated data.
