# Local HA Agent (Ollama)

Chat agent for Home Assistant backed by a local Ollama server (or any cloud model via LiteLLM).
Runs as a HA add-on with an ingress-based chat UI.

## Architecture

```
HA Supervisor host (add-on container)
  ├─ FastAPI app (app/main.py) — chat UI + /api/chat + /api/health
  ├─ LangGraph ReAct agent (app/agent.py) — tool registry with tier gating
  ├─ Tool registry / tiers (app/tools/) — 8 read tools, tier 1 default
  ├─ Skills loader (app/skills/*.md) — Markdown prompt fragments injected at runtime
  ├─ WS client (app/ha_ws.py) — WebSocket connection to HA, cached entity state
  ├─ LLM factory (app/llm_factory.py) — ChatOllama or LiteLLM, model knobs forwarded
  └─ REST adapter (app/ha_client.py) — GET-only via http://supervisor/core
```

## Dev Setup

```bash
python -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

Create `.env` (never committed):

```
HA_BASE_URL=http://homeassistant.local:8123
HA_TOKEN=<long-lived token>
LLM_URL=http://192.168.1.50:11434
LLM_MODEL=qwen2.5:7b
```

Run CLI:

```bash
venv/bin/python -m app.cli
```

**To save conversations**
```bash
venv/bin/python -m app.cli --save-conversations
```

Run server locally:

```bash
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8099
```

## Eval Harness

Before relying on the agent, verify tool-calling actually works — not every model
reliably calls tools instead of answering in prose.

```bash
venv/bin/python -m tests.evals.run
```

## Installing as a Local Add-on

1. Copy this folder to `/addons/local/local_ha_agent` on your HA host.
2. In HA: Settings → Add-ons → Add-on Store → ⋮ → Check for updates.
3. Install, then go to Configuration and set at minimum:
   - `llm_url`, `llm_model`, and optionally `api_key` for cloud providers.
4. Start — ingress opens the chat UI directly in the HA sidebar.

## Key Options (`/data/options.json` → `Settings` fields)

| Key | Default | Notes |
|---|---|---|
| `llm_provider` | `ollama` | `ollama` or LiteLLM provider name |
| `llm_url` | `http://192.168.1.50:11434` | Ollama URL, or llama.cpp/OpenAI-compat base URL |
| `llm_model` | `qwen2.5:7b` | Model tag |
| `api_key` | `` | Cloud provider key (stored as password) |
| `max_tier` | `1` | `1` = read-only; `2` = control tools (iteration 2) |
| `allowed_domains` | `["light","switch","automation"]` | Domains the agent may control when `max_tier=2` |
| `temperature` | `0.0` | |
| `recursion_limit` | `15` | Max agent steps per request |
| `max_rows` | `50` | Max entity rows returned per tool call |

Setting `max_tier: 2` unlocks the `control_entity` and `trigger_automation` tools, letting the agent turn lights on/off, toggle switches, and fire automations. The `allowed_domains` list is the hard boundary — the REST client refuses writes to any domain not in it, so removing `automation` from the list disables that capability entirely. All tier-2 actions are logged to `/data/audit.db` (SQLite, append-only) so you have a durable record of what the agent changed.

## Iteration Roadmap

- **Iteration 2:** Control tools (`call_service`, `control_entity`) behind `max_tier=2` + `allowed_domains` enforcement.
- **Iteration 3:** Assist pipeline integration + streaming UI.
