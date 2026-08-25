# Needle host probe

Run inside the throwaway container (after `pip install cactus-needle`). Copy the
whole block from `python` through `PY`.

```bash
python - <<'PY'
import time, needle

@needle.tool
def goodnight():
    "Start the bedtime routine: turn off all lights and lock the doors."
    return {"ok": True}

@needle.tool
def movie_time():
    "Start movie night: dim the living room lights and turn on the TV."
    return {"ok": True}

t0 = time.time()
agent = needle.Needle(tools=[goodnight, movie_time])
print(f"model load: {time.time()-t0:.2f}s")

for q in ["goodnight", "let's watch a movie", "what's the temperature?"]:
    agent.reset()
    t0 = time.time()
    out = agent.complete(q)
    calls = out.get("function_calls") or []
    name = calls[0]["name"] if calls else "(none)"
    print(f"{q!r:26} -> {name:11} conf={out.get('confidence'):.3f}  {(time.time()-t0)*1000:.0f}ms")
PY
```