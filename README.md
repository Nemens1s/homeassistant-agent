# Agent Gosling

Chat agent for Home Assistant backed by a local Ollama server (or any OpenAI-compatible cloud model).
Runs as a HA App with an ingress-based chat UI and optionally as a voice assistant via Assist.

## Install

### App (Add-on)

1. In HA: Settings → Add-ons → ⋮ → Repositories → add `https://github.com/Nemens1s/homeassistant-agent`
2. Agent Gosling appears under the custom repository — Install it.
3. On the **Configuration** tab set at minimum `llm_url` and `llm_model`, then Start.
4. The sidebar panel opens the chat UI through Ingress.

### Assist companion component

Lets Agent Gosling act as the conversation agent in HA voice pipelines.

1. HACS → Custom repositories → add `https://github.com/Nemens1s/homeassistant-agent` → category: Integration → Install, then restart HA.
2. Settings → Devices & Services → Add Integration → **Agent Gosling**. Set the App base URL (default `http://local-ha-agent:8099`).
3. Settings → Voice assistants → select **Agent Gosling** as the conversation agent.

**Security note:** the component→App hop is unauthenticated and assumes both sit in the same LAN trust domain. If the App port is ever exposed beyond the host, put an API token in front of it first.

**Assist wants a fast model:** the component uses a 90 s HTTP timeout and Assist pipelines favor fast responses — a slow answer stalls the whole voice turn. Long-running questions belong in the chat UI, not Assist.

## Key Options

| Key | Default | Notes |
|---|---|---|
| `llm_provider` | `ollama` | `ollama` for local; anything else uses ChatOpenAI |
| `llm_url` | — | Ollama URL, or any OpenAI-compatible base URL |
| `llm_model` | — | Model tag served by the LLM backend |
| `api_key` | `` | Cloud provider key (stored as password) |
| `max_tier` | `1` | `1` = read-only; `2` = control tools |
| `allowed_domains` | `["light","switch","automation"]` | Domains the agent may control at `max_tier=2` |
| `temperature` | `0.0` | |
| `recursion_limit` | `15` | Max agent steps per request |
| `max_rows` | `50` | Max entity rows returned per tool call |
| `checkpoint_db_path` | `/data/checkpoints.sqlite` | SQLite conversation persistence; empty = in-memory |

Setting `max_tier: 2` unlocks `control_entity` and `trigger_automation`, letting the agent turn lights on/off, toggle switches, and fire automations. `allowed_domains` is the hard boundary — the REST client refuses writes to any domain not in the list. All tier-2 actions are logged to `/data/audit.db` (SQLite, append-only).

## Streaming & persistence

The chat UI streams over Server-Sent Events from `POST /api/chat/stream`. Set `checkpoint_db_path` to persist conversations across App restarts; leave it empty for the in-memory default.

## Standalone (non-App) deployment

The same image runs as a plain Docker container configured via `.env` — for example on the PC, reachable over the LAN. Point the component's base URL at that host.

## Dev Setup

```bash
uv sync        # creates .venv and installs all deps from uv.lock
```

Create `.env` (never committed):

```
HA_BASE_URL=http://homeassistant.local:8123
HA_TOKEN=<long-lived token>
LLM_URL=http://192.168.1.50:11434
LLM_MODEL=qwen2.5:7b
```

```bash
uv run python -m app.cli                          # capability REPL
uv run python -m tests.evals.run                  # tool-selection scoring
uv run uvicorn app.main:create_app --factory --port 8099  # local server
```

Deploy to HA during dev (needs SSH access):

```bash
HA_SSH_HOST=192.168.1.x HA_TOKEN=<token> ./scripts/deploy.sh --component --restart
```

## Architecture

```
HA Supervisor host (App container)
  ├─ FastAPI app (app/main.py) — chat UI + /api/chat + /api/health
  ├─ LangGraph ReAct agent (app/agent/) — tool registry with tier gating
  ├─ Tool registry / tiers (app/tools/) — read + control tools
  ├─ Skills loader (app/skills/*.md) — Markdown prompt fragments injected at runtime
  ├─ WS client (app/ha/websocket.py) — WebSocket connection to HA, cached registries
  ├─ LLM factory (app/agent/llm.py) — ChatOllama or ChatOpenAI, model knobs forwarded
  └─ REST adapter (app/ha/rest.py) — GET-only (+ allowlisted writes at tier 2)
```
