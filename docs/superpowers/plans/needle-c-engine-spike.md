# Cactus C-engine spike

> **OUTCOME (2026-08-25): build FAILS on x86-64 — parked.** `cactus build
> --python` feeds an ARM march flag to the x86 compiler:
> `cc1plus: error: bad value 'armv8.2-a+fp16+simd+dotprod+i8mm' for '-march='`.
> The kernel CMake is hardcoded for ARM (mobile-first); no x86 build path out of
> the box. Would need upstream CMake patching (or a prebuilt x86 engine binary).
> Decision: ship the JAX/pip path instead — see `needle-spike-notes.md`. Recipe
> kept below in case they add x86 support later.

Goal: does the C/C++ cactus engine **build**, **run on the no-AVX host**, expose
**confidence**, and at **what latency** vs the JAX path (which was ~2–4 s).

Run on the HA host. Debian container = faithful CPU + easy build (no musl fight).

## 1. Start a container on the host

```bash
docker run --rm -it python:3.12-bookworm bash
```

## 2. Build the engine + Python bindings (inside the container)

```bash
apt-get update && apt-get install -y git cmake build-essential libcurl4-openssl-dev
git clone https://github.com/cactus-compute/cactus && cd cactus
source ./setup
cactus build --python
```

(The build compiles C/C++ — expect a few minutes. If `source ./setup` created a
venv, stay in this same shell so the `cactus` python module is importable.)

## 3. Run the probe (same tools/queries as the JAX test, for comparison)

Copy the whole fenced block:

```bash
python - <<'PY'
import json, time
from cactus import ensure_model, cactus_init, cactus_complete, cactus_destroy

tools = json.dumps([
  {"type":"function","function":{"name":"goodnight",
    "description":"Start the bedtime routine: turn off all lights and lock the doors.",
    "parameters":{"type":"object","properties":{},"required":[]}}},
  {"type":"function","function":{"name":"movie_time",
    "description":"Start movie night: dim the living room lights and turn on the TV.",
    "parameters":{"type":"object","properties":{},"required":[]}}},
])

t0 = time.time()
bundle = ensure_model("Cactus-Compute/needle")
model = cactus_init(str(bundle), None, False)
print(f"model load: {time.time()-t0:.2f}s")

def pick(r):
    calls = r.get("function_calls") or []
    if not calls:
        return "(none)"
    c = calls[0]
    return c.get("name") or c.get("function", {}).get("name", "?")

first = True
for q in ["goodnight", "let's watch a movie", "what's the temperature?"]:
    msgs = json.dumps([{"role":"user","content":q}])
    t0 = time.time()
    raw = cactus_complete(model, msgs, None, tools, None)
    r = json.loads(raw) if isinstance(raw, str) else raw
    if first:  # show the raw shape once so we confirm the fields
        print("RAW:", json.dumps(r)[:400])
        first = False
    print(f"{q!r:26} -> {pick(r):11} conf={r.get('confidence')}  {(time.time()-t0)*1000:.0f}ms")

cactus_destroy(model)
PY
```

## What to paste back

- The `model load:` line, the `RAW:` line, and the three result lines (or any
  build/import error).
- If `from cactus import ...` fails, run `python -c "import cactus; print(dir(cactus))"`
  and paste that so I can correct the API names.

## How we'll read it

- Builds + runs + shows `confidence` + latency clearly under the JAX path's
  ~2–4 s → **C engine wins**: ship Debian base + C engine (small image, fast),
  I write `CactusEngineBackend`.
- Builds but latency ≈ JAX → not worth the build complexity; fall back to the
  pip/JAX-on-Debian path (CactusBackend is already done).
- Build fails / import weird → paste the error and we adjust.
