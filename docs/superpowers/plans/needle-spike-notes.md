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

## Host results (2026-08-25) — amd64 HA VM, no AVX exposed

Ran the described-tools probe inside a throwaway `python:3.12` container in the
HA VM (`pip install cactus-needle`). CPU flags showed **no `avx` at all** (likely
Proxmox `kvm64`), yet **inference ran without SIGILL**:

```
model load: 3.56s
'goodnight'                -> goodnight   conf=0.969  2849ms
"let's watch a movie"      -> movie_time  conf=0.001  4194ms
"what's the temperature?"  -> (none)      conf=0.995  1918ms
```

- **The JAX/pip path RUNS on the no-AVX host** — jaxlib 0.11.1 has a scalar
  fallback; AVX is not required. Deployment is just `pip install cactus-needle`,
  no C build.
- **Latency ~1.9–4.2 s** per call (scalar XLA; ~40–100× slower than the arm64
  NEON path). Worse than the ~1–2 s goal but well under the >10 s agent — user
  accepts it for the common case. Variance is partly XLA recompiles on differing
  input shapes; could stabilise with padding.
- Confidence calibration matches arm64: correct→high, and the sub-threshold
  `movie` case correctly falls through.

## Proxmox CPU-type gotcha (IMPORTANT for future-you)

The HA OS VM's exposed CPU is governed by the **Proxmox VM CPU *type***, NOT the
physical chip. The default (`kvm64`) presents only x86-64-v1 (SSE2) — no SSE4.2,
no POPCNT, no AVX — even though the Mac mini is Sandy Bridge (2011) which has
SSE4.2 **and** AVX1. Symptoms this caused during the spike:

- `grep avx /proc/cpuinfo` returned **empty**.
- numpy (X86_V2-baseline wheel) refused to import:
  `RuntimeError: NumPy was built with baseline optimizations (X86_V2) but your
  machine doesn't support (X86_V2)`.

**Fix:** Proxmox → HAOS VM → Hardware → Processor → **Type = `host`** (exposes
everything incl. AVX1; needs a VM restart, brief HA downtime). Verify:

```bash
grep -o -m1 'sse4_2\|popcnt\|avx[0-9]*' /proc/cpuinfo | sort -u
# -> avx  popcnt  sse4_2
```

Tradeoff: `host` breaks live-migration portability — irrelevant on a single
host. This unblocked numpy, but note it did **not** speed up JAX (below).

## Host results after CPU=host (2026-08-25)

- **JAX path: no change** — still 1.7–4.2 s (`goodnight` 2930ms, movie 4231ms,
  temperature 1736ms). AVX1 alone doesn't help XLA; its fast kernels want AVX2,
  which Sandy Bridge lacks → scalar fallback regardless. **The JAX path is stuck
  at ~2–4 s; no CPU config fixes it.**
- **C engine: build FAILS on x86.** `cactus build --python` feeds an ARM march
  flag to the x86 compiler:
  `cc1plus: error: bad value 'armv8.2-a+fp16+simd+dotprod+i8mm' for '-march='`.
  Cactus's kernel CMake is hardcoded for ARM (mobile-first) and doesn't build for
  x86-64 out of the box. Making it build would require patching upstream CMake —
  out of scope. (A prebuilt x86 engine binary, if they ever publish one, would
  be the alternative.)

## Decision

**Approach A, via the `cactus-needle` pip/JAX backend, in-process, on a Debian
(glibc) add-on base.** It is the only runtime that runs end-to-end on the target.
Latency ~2–4 s — accepted (beats the >10 s agent). Deployment: switch
`build.yaml` to the HA `-base-debian` image, `Dockerfile` apk→apt, add
`cactus-needle` to `requirements.txt`. `CactusBackend` (in-process) is already
implemented + tested.

**C engine: parked, not viable now** — blocked on upstream x86-64 build support
(ARM-only kernel march). Revisit only if they publish x86 binaries or fix the
build; it stays a drop-in `NeedleBackend` swap if so.

**Possible future latency lever:** the 1.7–4.2 s variance looks like XLA
recompiles per input shape. Padding the prompt to a fixed length so XLA compiles
once (plus a warm-up call at startup) may lower steady-state latency — untested.

## Confident-fraction eval (2026-08-25) — the go/no-go on VALUE

Ran `tests/evals/run_needle.py` (4-automation menu, 9 utterances) — confidence
is a model property, so measured on arm64. **Verdict: zero-shot is not reliably
useful.**

- Confident-fraction **2/9 (~22%)** at any threshold in [0.7, 0.85]. Only
  near-exact lexical matches fire (`goodnight` 0.956, `start movie night` 0.987);
  canonical generalisations fail (`good morning`→movie 0.119, `time for bed`→
  movie 0.000, `let's watch a film`→movie 0.438).
- **Brutally brittle**: one shared token wrecks it — a goodnight description
  ending "…for the night" + a "Movie night" description made `goodnight`→movie
  at 0.006. Tool ORDER also swings confidence (goodnight 0.980 → 0.433 just moved
  to last). Descriptions must be imperative and share no tokens across tools.
- Odd bias: when unsure it dumps onto one tool (movie, the 2nd) at low confidence.
- **Safety holds**: no read ever false-fired; every wrong pick was low-confidence
  → falls through to the agent. Never dangerous, just mostly unhelpful zero-shot.

**Implication:** the realistic path to value is **fine-tuning Needle** (LoRA, which
it supports) on the user's automations + phrasing variations — NOT more
prompt/description fiddling, and NOT a threshold change. Recommendation: do NOT
do the Debian base-image migration for a zero-shot deploy. Either invest in a
fine-tuning spike first, or shelf until fine-tuning / newer hardware. The eval
harness (`run_needle.py`) is the yardstick for any fine-tune.
