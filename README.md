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
language-layer tuning, `telemetry_enabled`, the `telemetry_retention_days`/
`thread_retention_days` pruning windows, and the `memory_note_*`/
`time_note_grace_minutes` note-scheduling knobs) stays in the add-on schema
with a code default and is hidden from the Configuration tab until you add
it yourself. `checkpoint_db_path`, `telemetry_db_path`, and `glossary_path`
are no longer configurable at all — the add-on fixes them to `/data` and
`/config/gosling` paths via environment variables in `run.sh`. `disabled_tools`
(a list of tool names) switches specific tools off regardless of tier or
whether a matching entity exists in this home. `skills_enabled` (hidden,
default `false`) turns on user-supplied troubleshooting playbooks — see
"Skills (optional)" above.

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

## Degraded mode (LLM offline)

Gosling probes its LLM server (llama.cpp `/health`, Ollama `/api/version`,
others `/models`) every 15 s while it is down and every 60 s while it is up,
and marks it down immediately when a request fails to connect. While the LLM
is down:

- Requests fail in about 3 s instead of hanging, and the web UI shows an
  "LLM offline — command mode" banner.
- Typed text is matched against your `ai_*` action names. One clear match
  runs (still subject to the AI-actions switch) when every word you typed is
  part of the action's name and the text is not a question ("is the garage
  door open?" never opens it); otherwise you get a numbered "did you mean"
  list. Anything else gets an "LLM offline" reply.
- Requests about later ("when…", "at 18:00…") are refused, never run now.

Slash commands work at any time, LLM or not, and always answer in English:
`/actions`, `/run <number or name> [key=value …]`, `/notes`,
`/cancel <id>`, `/help`. They are not saved in the conversation.

If the fast path is configured, its `/health` is probed the same way; while
it is down, requests skip it instead of waiting out its timeout.
`/api/health` reports `llm_state`, `llm_since`, `fast_path_state` and always
returns HTTP 200 (so the Supervisor watchdog never restart-loops the add-on).

## Tools

The agent's tool menu is assembled once at add-on start: every tool allowed
by `allow_actions` (tier), not named in `disabled_tools`, and — for a few
helper tools — backed by a matching entity in this home.

Core tools (always offered, subject only to tier and `disabled_tools`):

| Tool | Needs |
|---|---|
| `get_entity_state` | REST |
| `list_entities` | REST + WS registries |
| `get_history` | REST |
| `get_activity` | REST (logbook) |
| `get_error_log` | REST |
| `get_areas` | WS registries |
| `get_automations` | REST |
| `list_actions` | REST |
| `search_entities` | REST + WS registries |
| `list_devices` | WS registries |
| `schedule_on_state_change` / `schedule_at_time` / `list_scheduled` / `cancel_scheduled` | agent-local memory store |
| `trigger_action` / `notify_user` | REST; ACTION tier; gated behind `ai_actions_switch` |

Helper tools register only when this home has something for them to use:

| Tool | Registered when |
|---|---|
| `get_vacuum_state` | a `vacuum.*` entity exists |
| `get_battery_status` | an entity with `device_class: battery` exists |
| `get_weather` | a `weather.*` entity exists |
| `get_person_locations` | a `person.*` entity exists |
| `load_skill` | `skills_enabled: true` and at least one skill file in the skills folder parses — see "Skills (optional)" below |

These checks run once at add-on start. After adding a device that should
unlock one of these tools, restart the add-on. If Home Assistant can't be
reached at startup, every tool is registered anyway — a tool with nothing
behind it then returns a clear error instead of silently disappearing.

`disabled_tools: [get_weather, ...]` turns off any tool by name, core or
helper — the obvious result is the agent can no longer do that thing.

### Skills (optional)

Skills are step-by-step troubleshooting playbooks the agent can load by name
with `load_skill` (e.g. "my automation didn't fire last night — why?").
Nothing ships in the image: drop your own markdown files, each starting with
a frontmatter block giving `name` and `description`, into the skills folder
(default `/config/gosling/skills`) and set the hidden `skills_enabled`
option to `true`. See `docs/examples/skills/` for the format. Skills are
prompt text only — they add troubleshooting steps the agent can follow, and
cannot widen what it can do beyond `automation.ai_*`/`script.ai_*`.

`get_vacuum_state` discovers its own entities: it uses the first `vacuum.*`
entity (sorted by entity_id) and looks for a room sensor on the same device.
Set the hidden `vacuum_entity` option to pick a specific vacuum when this
home has more than one. The room is left out of the answer when no
`_current_room` sensor exists on that device.

Each tool skipped by a probe is logged once at startup as
`tool=<name> not registered: nothing in this home to use it on`.

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
  ├─ Skills loader (app/skills/) — parses user-supplied Markdown playbooks from
  │  skills_path at runtime; off by default (skills_enabled)
  ├─ WS client (app/ha/websocket.py) — WebSocket connection to HA, cached registries
  ├─ LLM factory (app/agent/llm.py) — ChatOllama or ChatOpenAI, model knobs forwarded
  └─ REST adapter (app/ha/rest.py) — GET-only (+ allowlisted writes at tier 2)
```
