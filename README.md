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

1. HACS → Custom repositories → add `https://github.com/Nemens1s/hass-agent-gosling` → category: Integration → Install, then restart HA.
2. Settings → Devices & Services → Add Integration → **Agent Gosling**. Set the App base URL (default `http://agent-gosling:8099`).
3. Settings → Voice assistants → select **Agent Gosling** as the conversation agent.

**Security note:** the component→App hop is unauthenticated and assumes both sit in the same LAN trust domain. If the App port is ever exposed beyond the host, put an API token in front of it first.

**Assist wants a fast model:** the component uses a 90 s HTTP timeout and Assist pipelines favor fast responses — a slow answer stalls the whole voice turn. Long-running questions belong in the chat UI, not Assist.

## Key Options

The Configuration tab shows only this minimal set; everything else has a
sane default and stays hidden until you explicitly set it (see "Advanced
options" below).

| Key | Default | Notes |
|---|---|---|
| `llm_url` | `` | Ollama URL, llama.cpp (llama-server) URL, or any OpenAI-compatible base URL |
| `llm_model` | `` | Model tag served by the LLM backend |
| `api_key` | `` | Cloud provider key (stored as password) |
| `allow_actions` | `false` | `false` = read-only; `true` = allows `trigger_action`/`notify_user`/scheduling |
| `ai_actions_switch` | `input_boolean.ai_triggered_actions` | Home-level AI kill switch; empty disables it |
| `notify_action` | `script.ai_action_notify` | The only script `notify_user` may call |
| `extra_instructions` | `` | Household-specific text appended after the built-in base prompt (see `/api/prompt` on the Metrics tab) |
| `watched_entities` | `[]` | Entities the agent may watch for state-triggered notes; empty = off. Up to 30 are also given to the model with their current state on every message, so it can use them directly without a search tool call |
| `clock_entity` | `` | World Clock sensor for time-triggered notes/scheduling; empty = off |
| `event_confirmations_enabled` | `true` | Send a confirmation message when a fired note acts |
| `person_name_map` | `[]` | Rename people for the agent (`ha_name` → `name`) |
| `language_layer_enabled` | `false` | Accept non-English messages via the lang-mt translation service |
| `languages` | `[en]` | Languages detection may choose between; add `et`/`ru` etc. to enable them |

Setting `allow_actions: true` unlocks `trigger_action` and `notify_user`, letting the agent fire `automation.ai_*` / `script.ai_*` and send notifications. All action-tier calls are logged to `/data/audit.db` (SQLite, append-only) and gated behind `ai_actions_switch`.

### Advanced options

The base system prompt itself is code-owned (not configurable) — `extra_instructions`
is appended after it under a "HOUSEHOLD INSTRUCTIONS" heading. The full assembled
prompt (base + conditional sections + your `extra_instructions`) is viewable at
`GET /api/prompt`, and in a collapsible panel on the Metrics tab.

Everything else (LLM sampling knobs like `temperature`/`top_p`/`num_ctx`,
`llm_provider`, `recursion_limit`, `max_rows`,
`max_history_messages`, `enable_tool_subsetting`, the `fast_path_*` keys,
`person_name_exclude`, the `lang_mt_*`/`default_language`/`min_confidence`
language-layer tuning, `telemetry_enabled`, and the `memory_note_*`/
`time_note_grace_minutes` note-scheduling knobs) stays in the add-on schema
with a code default and is hidden from the Configuration tab until you add
it yourself. `checkpoint_db_path`, `telemetry_db_path`, and `glossary_path`
are no longer configurable at all — the add-on fixes them to `/data` and
`/config/gosling` paths via environment variables in `run.sh`. `disabled_tools`
(a list of tool names) switches specific tools off regardless of tier or
whether a matching entity exists in this home.

If you run a multilingual model without turning on the language layer,
set `enable_tool_subsetting: false` — the small-model tool-subsetting
heuristic is tuned on English tool descriptions and can mis-rank tools for
non-English prompts.

**Provider auto-detection:** with `llm_provider: auto` (the default), the app
probes `llm_url` once at startup — `/api/tags` for Ollama, `/props` for
llama.cpp (llama-server) — and falls back to a plain OpenAI-compatible client
if neither answers or `api_key` is set. For llama.cpp specifically, `num_ctx`
is also read from `/props` (`default_generation_settings.n_ctx`) and used
automatically, so you don't need to duplicate the server's context size in
options. The probe never blocks startup indefinitely (3 s timeout per
request) and never fails the add-on — an unreachable backend just falls back
to the OpenAI-compatible client, same as before this behavior existed.

## Fast path

An optional low-latency path: an external inference service maps a user
message straight to one `ai_*` action, skipping the LLM entirely. Off by
default (`fast_path_enabled: false`); requires `allow_actions: true` as well,
since it only ever triggers an action.

Only automations/scripts whose HA description opts in are eligible. Add a
`FAST-PATH:` marker to the description — the text after the marker is what
the service matches requests against:

```yaml
alias: "AI: start vacuum"
id: ai_action_start_vacuum
description: "FAST-PATH: start the vacuum / clean the house"
triggers: []
actions:
  - action: vacuum.start
    target:
      entity_id: vacuum.my_vacuum
```

Set `fast_path_url` to the service's base URL; the app POSTs to `<url>/classify`:

```json
{
  "message": "turn on the vacuum",
  "tools": [
    {"name": "start_vacuum", "description": "start the vacuum / clean the house", "parameters": {}}
  ],
  "signature": "<menu cache signature>"
}
```

and expects a decision back as JSON:

```json
{
  "confidence": 0.0,
  "function_calls": [
    {"name": "start_vacuum", "arguments": {}}
  ]
}
```

`function_calls` empty or absent means "no match" (falls through to the
agent). `confidence` is optional; a missing value is treated as `0.0`. Set
`fast_path_confidence_threshold` to require a minimum confidence before
firing (default `0.0` fires on any emitted call — correct for models that
don't report a calibrated confidence); the router falls through to the
normal agent on any network error, a low-confidence decision, or no match.

| Key | Default | Notes |
|---|---|---|
| `fast_path_enabled` | `false` | Requires `allow_actions: true` as well |
| `fast_path_url` | `` | Base URL of the remote fast-path service |
| `fast_path_confidence_threshold` | `0.0` | Fire only at/above this confidence |

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
LLM_URL=http://llm_url:llm_port
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
