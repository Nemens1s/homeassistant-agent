# Confidence scores on fine-tuned Needle weights

**Status: no confidence gate on tuned weights (validated dead cheaply, 2026-08-25).**
The fast path runs *trust-the-call* (fire on emitted call, fall through on none).
This doc records why, so the cheap path isn't re-chased, and lists the real
options if a genuine confidence signal ever becomes worth the effort.

## Why we wanted it

The original design gated the fast path on a calibrated confidence: fire when
sure, **fall through to the full agent when unsure**. That graceful abstention is
lost with tuned weights — a mis-pick now *fires* (the wrong one of your own
`automation.ai_*`) instead of deferring. A working confidence score would bring
abstention back.

## How Needle's confidence works

`confidence = min(head_score, decode_prob)`:

1. **`ConfidenceHead`** (`needle/model/architecture.py`): 8 learnable "probe"
   vectors attention-pool the transformer's hidden states → `Dense(1)` → a logit
   → a calibrated "is this output correct/grounded?" score. A *separate head*,
   calibrated on the **base** model's training mix. Exported into the `.cact`
   (head code 2: probes + proj + bias).
2. **Decode probability**: the model's own probability of the emitted call
   tokens. Intrinsic to any weights.

Fine-tuning (LoRA) updates the base weights but **not** the head, so the head's
learned mapping no longer matches the shifted hidden-state distribution. The
package guards against trusting stale scores with a one-line veto
(`needle/__init__.py`):

```python
if self._weights:                 # tuned weights loaded
    response["confidence"] = None
```

The C engine still *computes* a value; the Python wrapper just discards it.

## What we tried (the cheap path) and why it failed

**Idea:** bypass the veto, read the engine's raw (uncalibrated) confidence for
tuned weights, and gate on a threshold — accepting it's uncalibrated but hoping
the *relative* ordering still separates right from wrong.

**Method:** held-out eval (17 cases, the real 3-automation menu), `reset()` before
every query (critical — see lesson below), threshold sweep.

**Result: no separating threshold exists.** Both wrong answers scored as high as
correct ones:

| Query | Emitted | Raw confidence | Correct? |
|-------|---------|---------------|----------|
| turn on the concorde light | on | 1.0 | ✅ |
| please switch the concorde plug off | **on** | **1.0** | ❌ (should be off) |
| is anybody home right now? | **on** | **0.9865** | ❌ (should not fire) |
| what's the temperature in here? | (none) | 1.0 | ✅ |

The sweep held `WRONG_fire = 2` at every threshold that kept the correct fires;
only `0.99` dropped one wrong fire — while also discarding 4 correct ones. The
head is **confidently wrong** on fine-tune errors, which is the worst possible
failure for a gate. This is *why* the vendor disables it — not laziness.

**Lesson:** a first probe *without* `reset()` looked promising (errors appeared
low-confidence). That was conversation-state leaking between calls in the engine's
sliding window. **Always `reset()` between independent queries** when measuring —
otherwise the numbers are contaminated.

## Possible solutions (if confidence ever becomes worth it)

Roughly best-to-worst for our use:

1. **Recalibrate the head post-fine-tune (principled).** After training, fit a
   calibration on the tuned model's own outputs: run a labeled held-out set
   through `forward_confidence`, then temperature/Platt-scale (or retrain just the
   head's `proj`/`bias`) so scores match observed correctness. Effort: moderate;
   needs custom code against needle internals + a labeled set. Tooling doesn't
   ship it. This is the "correct" fix.
2. **Train the head jointly during fine-tuning.** Unfreeze the confidence head in
   `needle finetune` and give it a correctness target. Requires patching upstream
   training + confidence labels in the dataset. More invasive, upstream-ish.
3. **External verifier (decoupled).** Ignore Needle's head entirely; train a
   separate tiny classifier over (prompt + proposed call) → fire/abstain, with
   calibration you own. Independent of Needle internals; effort = build + train a
   small model, but fully under your control.
4. **Decode-probability alone.** The intrinsic half survives fine-tuning. But our
   combined value (which already includes it) was ~1.0 on confident-wrong cases,
   so decode-prob alone is unlikely to separate them. Cheap to try, probably
   insufficient — measure before believing.
5. **Design-level mitigation instead of a score.** Keep trust-the-call; reduce
   errors with more/better training data (more polarity + device-mentioning read
   negatives), and lean on the structural gates (menu-only + `ai_actions_switch` +
   per-automation self-condition) to bound blast radius. **This is the current
   stance** — cheapest, and errors stay "wrong own-automation," never dangerous.

## Recommendation

Stay on (5) for now. If a real confidence gate becomes important (bigger menu,
higher-stakes actions), pursue (1). Validate any approach with the **same
held-out threshold-sweep method, `reset()` per case**, and require it to drive
`WRONG_fire` to ~0 while keeping most correct fires — the bar the cheap path
failed. See `docs/needle-finetuning.md` and
`docs/superpowers/plans/needle-spike-notes.md`.
