# Needle Remote Server — API Contract

The `RemoteNeedleBackend` in `app/needle/remote_backend.py` delegates inference to an HTTP server
running on a capable box (ASUS, dedicated GPU host, etc.). This document is the contract both
sides must implement.

## Endpoint

```
POST /classify
Content-Type: application/json
```

## Request

```json
{
  "message": "goodnight",
  "tools": [
    {
      "name": "goodnight",
      "description": "Start the bedtime routine: turn off all lights and lock the doors."
    },
    {
      "name": "movie_time",
      "description": "Dim the lights and start the TV."
    }
  ],
  "signature": "3a7f2c…"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `message` | string | The raw user utterance to classify. |
| `tools` | array | One entry per `ai_*` automation currently in the HA menu. The server builds a `needle.Needle(tools=...)` agent from this list. |
| `tools[].name` | string | Python-identifier tool name derived from the automation entity_id (`automation.ai_goodnight` → `goodnight`). |
| `tools[].description` | string | Automation friendly name or description. Needle matches on name + description — quality here directly affects confidence. |
| `signature` | string | SHA-256 of the sorted entity_id set. The server should cache its compiled `needle.Needle` agent keyed by this value and rebuild only when it changes. |

## Response

**200 OK — match found:**
```json
{
  "function_calls": [{"name": "goodnight", "arguments": {}}],
  "confidence": 0.969
}
```

**200 OK — no match:**
```json
{
  "function_calls": [],
  "confidence": 0.995
}
```

| Field | Type | Description |
|-------|------|-------------|
| `function_calls` | array | Zero or one entry. If the model emits a tool call, the entry's `name` must be one of the names sent in the request. |
| `function_calls[].name` | string | The chosen tool name (grammar-constrained by needle — will always be valid). |
| `function_calls[].arguments` | object | Needle emits `{}` for no-arg tools; the client ignores this field. |
| `confidence` | float | Model confidence in the call (0–1). The HA agent applies its own threshold gate and will fall through to the LLM agent if confidence is below `needle_confidence_threshold`. |

**Non-2xx** — any HTTP error causes the `RemoteNeedleBackend` to raise, which the `FastPathRouter`
catches, logs, and converts to a fall-through to the LLM agent. The server may return 4xx/5xx
with any body.

## Server-side implementation notes

The server is not part of this repo. A minimal implementation using FastAPI:

```python
import hashlib, needle as nd
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
_cache: dict[str, tuple] = {}   # signature -> (agent, tools_snapshot)

class Tool(BaseModel):
    name: str
    description: str

class ClassifyRequest(BaseModel):
    message: str
    tools: list[Tool]
    signature: str

@app.post("/classify")
async def classify(req: ClassifyRequest):
    if req.signature not in _cache:
        tools = []
        for t in req.tools:
            def fn(): return {"ok": True}
            fn.__name__ = t.name
            fn.__doc__ = t.description
            tools.append(nd.tool(fn))
        _cache[req.signature] = nd.Needle(tools=tools)
    agent = _cache[req.signature]
    agent.reset()
    result = agent.complete(req.message)
    return {
        "function_calls": result.get("function_calls") or [],
        "confidence": result.get("confidence") or 0.0,
    }
```

## Configuration (HA agent side)

```
NEEDLE_BACKEND=remote
NEEDLE_REMOTE_URL=http://192.168.1.x:8765
NEEDLE_ENABLED=true
MAX_TIER=2
```

Or in the addon UI: set **Needle backend** to `remote` and **Needle remote URL** to the server
address. `needle_confidence_threshold` defaults to `0.0` (fire on any emitted call); raise it
only when running the calibrated base model, not fine-tuned weights.

## Measured latency

| Host | 'goodnight' | 'let's watch a movie' | 'what's the temperature?' |
|------|------------|----------------------|--------------------------|
| Mac Mini (scalar XLA, no AVX) | 2849 ms | 4194 ms | 1918 ms |
| ASUS box | 139 ms | 208 ms | 86 ms |

The ASUS numbers include round-trip HTTP overhead measured from the client. Model load is a
one-time cost on server startup (~3.3 s); subsequent calls hit the cached agent.
