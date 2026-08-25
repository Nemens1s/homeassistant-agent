# Needle feasibility spike — findings (Task 1)

Date: 2026-08-25. Runtime probed on the dev machine (Apple Silicon, macOS,
Python 3.14). **The decisive amd64/no-AVX check on the HA host is still
PENDING** — see "Go/no-go" below.

## Package & dependencies

- `pip install cactus-needle` (extras: `[metal]` Apple Silicon, `[gpu]` NVIDIA).
- Installed **`cactus-needle 2.0.3`**, which pulls **`jax` / `jaxlib` 0.11.1**,
  `flax`, `optax`, `orbax`, `tensorstore`, `sentencepiece`, `numpy`, `scipy`.
- Import module is `needle` (not `cactus`). The base model is downloaded from
  the HF Hub on first `Needle(...)` construction — **no explicit `weights=`
  needed** to use the base needle2. `weights=<path.cact>` is only for a
  fine-tuned export. ⇒ `needle_model_path` can stay empty for the base model.

## API (verified)

```python
import needle
from typing import Literal

@needle.tool
def goodnight():
    "Start the bedtime routine: turn off all lights and lock the doors."
    return {"ok": True}

agent = needle.Needle(tools=[goodnight])      # loads/downloads base weights
out = agent.complete("goodnight")             # PARSE ONLY — does not execute tools
```

`Needle.__init__(self, tools=None, system=None, weights=None, tool_index_path=None, buffer_size=65536)`
Methods: `complete(text, max_new_tokens=256)`, `run(query, max_steps=8, max_new_tokens=256)`, `extract(text, schema, ...)`, `reset()`, `tool(fn)`.

**`complete()` return shape** (the parse-only method we use):

```python
{
  'type': 'call',
  'success': True,
  'function_calls': [{'name': 'goodnight', 'arguments': {}}],  # [] if no call
  'reasoning': "...",
  'confidence': 0.914,        # <-- the threshold signal
  'prefill_tps': ..., 'decode_tps': ..., 'peak_ram_mb': 127.2,
  'validation': {'ungrounded': [], 'negation': False},
}
```

- `run()` additionally *executes* the decorated tool and returns `results` —
  **do not use it**; we only want the proposal.
- `complete()` returned `function_calls` with **no `results` key** ⇒ confirmed
  side-effect-free.

## Key design finding: described tools, not an opaque enum

First attempt modelled a single `trigger_automation(entity_id: Literal[...])`
tool. The grammar constrained the output correctly, BUT confidence was near
zero because the opaque `automation.ai_*` ids give Needle nothing semantic to
match on (the model's own `reasoning`: *"No description given, so omitted."*):

| Utterance | Picked | Confidence |
|-----------|--------|-----------|
| `goodnight` | automation.ai_goodnight (correct) | **0.113** |
| `time for a movie` | (none) | 0.754 |

Second attempt modelled **one described `needle.tool` per automation** (name +
one-line description). Dramatically better and it validates the threshold
strategy:

| Utterance | Picked | Confidence | Outcome @ 0.85 |
|-----------|--------|-----------|----------------|
| `goodnight` | goodnight | **0.914** | ✅ fires (correct) |
| `what's the temperature?` | (none) | 1.000 | ✅ falls through (correct) |
| `I'm going to bed` | movie_time (**wrong**) | **0.001** | ✅ falls through — wrong answer rejected |
| `let's watch a film` | movie_time (correct) | 0.309 | falls through (correct-but-slow) |
| `time for a movie` | (none) | 0.669 | falls through (missed) |

**Takeaways:**
- High-confidence proposals were correct; the wrong mapping scored ~0 ⇒ the
  threshold sends wrong answers to the safe slow path. Fail-safe confirmed.
- Indirect phrasings score low and fall through (pay the agent cost). This is
  the confident-fraction KPI — tune with better descriptions / threshold /
  optional fine-tuning (Task 8 eval).
- Default threshold **0.85 looks sound**.

### Consequence for Task 6 (CactusBackend)

Interfaces from Tasks 3–5 are UNCHANGED (`Decision(entity_id, confidence)` is
all the router sees). Internally, `CactusBackend.classify(message, menu)` must:
1. Build one `@needle.tool` per `menu.items` entry — tool name derived from the
   automation (sanitised friendly_name), description from friendly_name (and a
   real description if we later surface one from the automation config), and a
   `{tool_name -> entity_id}` map.
2. Cache the `Needle(tools=...)` instance per `menu.signature` (rebuild only
   when the id-set changes).
3. `out = agent.complete(message)`; if `out["function_calls"]`, map the chosen
   name back to its `entity_id` and return `Decision(entity_id, out["confidence"])`;
   else `Decision(None, out["confidence"])`.

`menu.py` may later gain an optional `description` per `MenuItem`; friendly_name
is the v1 source.

## Latency (Apple Silicon dev box — NOT the target)

- Model load / first construction: **~3.8 s** (one-time; keep the agent warm).
- Per `complete()` call: **~25–75 ms**. Comfortably under the 1–2 s goal, but
  the amd64 host will differ — measure there.

## Go/no-go for approach A (in-process) — PENDING on the HA host

The runtime is **JAX/XLA-based**. jaxlib CPU wheels require **AVX**; XLA may
require **AVX2**. The HA host is a Sandy Bridge (AVX1, no AVX2) VM on Proxmox —
and Proxmox's default `kvm64` CPU type exposes **no AVX at all**. This is the
only thing that decides approach A vs B, and it can only be tested on that VM.

**Run on the HA host VM (a shell in the guest, amd64):**

```bash
# 1) What CPU flags does the guest actually see?
grep -o -m1 'avx[0-9_]*' /proc/cpuinfo | sort -u
#    (none)  -> set Proxmox VM CPU type to 'host' and retry, else approach B
#    avx only, no avx2 -> the import test below is decisive
#    avx2    -> very likely fine

# 2) Install into a throwaway venv and try to import + run one inference:
python3 -m venv /tmp/needle && /tmp/needle/bin/pip install -q cactus-needle
/tmp/needle/bin/python - <<'PY'
import time, needle
@needle.tool
def goodnight():
    "Start the bedtime routine."
    return {"ok": True}
a = needle.Needle(tools=[goodnight])
t0 = time.time(); out = a.complete("goodnight")
print("OK", out["function_calls"], round(out["confidence"],3),
      f"{(time.time()-t0)*1000:.0f}ms")
PY
```

- Prints `OK ...` → **approach A viable**; record the latency.
- `Illegal instruction (core dumped)` / SIGILL on import or run → XLA needs a
  SIMD level this CPU lacks → **approach B (sidecar)**, OR run Needle on a
  different box and point `needle_sidecar_url` at it (loses the "on the HA host"
  benefit but keeps the model-loading and network-hop wins if that box is fast).

## Decision

**Approach A pending the HA-host import test above.** API, parse-only mode,
grammar constraint, and the confidence/threshold strategy are all confirmed
working on arm64. The single open risk is the amd64 SIMD floor.
