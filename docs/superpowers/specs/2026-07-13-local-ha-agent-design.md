# Local Home Assistant Agent — Design Spec

**Date:** 2026-07-13
**Status:** Approved design, pre-implementation

## Overview

A Home Assistant add-on that runs a LangChain agent against a local Ollama
server (on a separate LAN machine), with optional cloud models via LiteLLM.
Iteration 1 is strictly read-only; iteration 2 adds bounded control
(lights/switches/automations). The architecture borrows the declarative tool
registry from the mylo project (`/Users/ilniko/IdeaProjects/mylo`) while
staying deliberately small — LangChain keeps the agent loop, memory, and
model bindings.

**Primary interface during iterations 1–2 is the CLI REPL** (capability
testing). The existing ingress chat UI stays functional but receives no
investment until iteration 3.

## Goals

- Answer questions about the home: entity states, history, logbook, areas,
  devices, automations, error log.
- Iteration 2: toggle lights/switches and enable/disable/trigger automations,
  bounded by a domain allowlist.
- Work reliably with small local models (7B class) — token budgets,
  determinism, and thinking control are first-class concerns.
- Swappable LLM backend: local Ollama by default, cloud providers via
  LiteLLM when needed.

## Non-goals (deliberately absent)

- Persistent memory / RAG over entity history.
- Proactive monitoring, anomaly detection, notifications (mylo territory).
- Multi-agent routing, per-user auth (ingress authenticates), LiteLLM
  fallback chains.
- Frontend polish, HTTP streaming (until iteration 3).
- Dashboard/config-file editing tools.

## Architecture

```
HA host (add-on container)                          Laptop
┌─────────────────────────────────────┐            ┌──────────┐
│ FastAPI (ingress) ── chat UI + API  │            │  Ollama  │
│   └─ LangChain agent (create_agent) │──────────▶│          │
│        ├─ LLM factory ──────────────│─(litellm)─▶ cloud APIs (optional)
│        └─ tools (from registry)     │            └──────────┘
│             ├─ REST client (httpx)  │──▶ http://supervisor/core/api
│             └─ WS client (persistent)──▶ ws://supervisor/core/websocket
└─────────────────────────────────────┘

Dev mode: CLI REPL on the dev machine → HA over LAN (long-lived token).
```

### Module layout

```
app/
├── main.py              # FastAPI app, lifespan (WS client start/stop), /api/chat, /api/health
├── cli.py               # REPL entrypoint (python -m app.cli) — same agent, streams output
├── config.py            # pydantic-settings: addon options.json (container) or .env (dev)
├── agent/
│   ├── factory.py       # build_agent(settings) → LangChain agent; filters tools by tier
│   └── llm.py           # build_llm(settings) → ChatOllama | ChatLiteLLM
├── ha/
│   ├── rest.py          # httpx client: GET endpoints; call_service() added in iteration 2
│   └── websocket.py     # persistent WS client: auth, id-correlated request(), reconnect
├── skills/              # markdown playbooks (name + description frontmatter)
└── tools/
    ├── base.py          # ToolDefinition, Tier, ToolResult, bound_rows
    ├── registry.py      # register(), load_all(), tools_for_tier(max_tier)
    ├── adapter.py       # ToolDefinition → LangChain StructuredTool
    ├── context.py       # ToolContext: rest + ws clients, settings, registry caches
    ├── read/            # tier-1 tools, one module per tool
    └── action/          # tier-2 tools (iteration 2)
```

Anti-patterns being removed from the current scaffold: no module-level
singletons (agent/LLM/clients are built by factories, config injected), and
no `str(python_dict)` tool output (compact JSON envelopes instead).

## Tool infrastructure

Adopted from mylo, trimmed:

- **`ToolDefinition`**: `name`, `description` (≤400 chars), `params_model`
  (pydantic v2), `tier`, async `handler(params, ctx) → ToolResult`. No
  provider schema converters — LangChain derives JSON schema from the
  pydantic model.
- **`Tier`**: `READ = 1`, `ACTION = 2`. The agent factory loads only tools
  with `tier <= settings.max_tier`. Iteration 1 ships `max_tier = 1`;
  tier-2 tools above the ceiling are never handed to the model — the
  read-only guarantee is "the tool does not exist," not a runtime check.
- **`ToolResult`**: `{status: ok|error, data, error_code?, error_message?}`.
  Handlers never raise into the agent loop.
- **`bound_rows`**: caps list results at `max_rows`, always reporting the
  true `total`, a `truncated` flag, and a "narrow your filter" hint.
- **`registry.py`**: `register()` at module import, `load_all()` imports the
  tool-module list, `tools_for_tier()` filters. Adding a tool = one new
  module + one line in the module list.
- **`adapter.py`** (replaces mylo's executor): converts each `ToolDefinition`
  into a LangChain `StructuredTool`. All cross-cutting behavior lives here:
  - pydantic validation errors → `invalid_params` envelope (model can retry),
  - exception mapping (see Error handling),
  - audit log line per call: tool name, params, duration, result status,
  - loop guard: an identical consecutive call (same tool, same params)
    short-circuits with an error envelope telling the model to change
    approach,
  - result serialized as compact JSON (no whitespace padding, no Python repr).
- **`ToolContext`**: carries the REST client, WS client, settings, and
  registry caches. Handlers never import clients directly — unit tests pass
  a fake context.

## Tools

### Iteration 1 (tier 1 — READ)

| Tool | Backend | Purpose |
|---|---|---|
| `get_entity_state` | REST | State + attributes of one entity |
| `list_entities` | REST + WS registries | Filter by domain and/or area; compact rows via `bound_rows` |
| `get_history` | REST | State changes for an entity over a time range |
| `get_logbook` | REST | Events/triggers in a time range |
| `get_error_log` | REST | Tail of the HA error log |
| `get_areas_and_devices` | WS | Area → device → entity topology |
| `get_automations` | WS + REST | List automations (state, last-triggered); fetch one config |
| `load_skill` | local FS | Load a skill playbook by name |

### Iteration 2 (tier 2 — ACTION)

- `control_entity(entity_id, action)` — `turn_on` / `turn_off` / `toggle`.
- `trigger_automation(entity_id)`.

One generic guarded tool, not per-domain tools. Enforcement is layered:
the handler checks `settings.allowed_domains` (default:
`["light", "switch", "automation"]`), **and** `rest.call_service()` — the
only write method on the REST client — independently refuses non-allowlisted
domains. No confirmation flow: worst case within the allowlist is a toggled
light.

## Skills (progressive disclosure)

`app/skills/*.md` with YAML frontmatter (`name`, `description`). The system
prompt gains a generated "Available skills" section — one line per skill.
The model calls `load_skill(name)` to pull the full playbook into context
only when relevant. Keeps the always-loaded prompt small for 7B models while
still encoding multi-step procedures (e.g. "diagnosing why an automation
didn't fire: state → traces → logbook → error log"). Skills are data, not
code — iterating on playbooks requires no code changes. Ship 1–2 seed skills
in iteration 1.

## LLM factory & model settings

`agent/llm.py`: if `llm_provider == "ollama"` → `ChatOllama` (best-tested
Ollama tool-calling); anything else (e.g. `anthropic/claude-…`) →
`ChatLiteLLM` (in-process `langchain-litellm`; no proxy server). Both return
a `BaseChatModel`; downstream code is provider-agnostic.

Model options (all in settings / addon options, tuned for small models):

| Option | Default | Why |
|---|---|---|
| `temperature` | `0` | Determinism |
| `seed` | `42` | Reproducible runs — evals become regression tests |
| `reasoning` | `false` | Disables `<think>` blocks on models that support it (qwen3); some (deepseek-r1) ignore it — model choice matters more |
| `num_predict` | `2048` | Hard ceiling per generation — the guard against runaway self-debate |
| `num_ctx` | `8192` | Ollama's default silently truncates tool schemas + prompt; set explicitly, treat as the token budget |
| `keep_alive` | `-1` | Keep model resident in RAM between requests (reload dominates latency on old hardware) |

Ollama-specific options are ignored when a LiteLLM provider is selected
(temperature/seed pass through where supported).

## Configuration

One pydantic-settings class (`config.py`). In the container it reads the
add-on `options.json` (+ `SUPERVISOR_TOKEN` env); in dev it reads `.env`
(`HA_BASE_URL`, `HA_TOKEN` long-lived token). Settings: `llm_provider`,
`ollama_url`, `model`, `api_key` (password-typed in the addon schema),
`max_tier`, `allowed_domains`, `system_prompt`, plus the model options
table above.

## Data flow (one chat turn)

UI/CLI → `/api/chat` (or REPL) → agent (checkpointed per `thread_id`,
history trimmed) → model emits tool call → adapter validates & executes →
handler via `ToolContext` → `bound_rows` truncation → compact JSON back to
model → (repeat, bounded by `recursion_limit`) → final answer. The CLI
streams tokens via `astream`; the HTTP endpoint returns the complete
response (streaming deferred to iteration 3).

## Robustness (all iteration 1)

1. **Conversation trimming** — token-based `trim_messages` to a budget
   derived from `num_ctx` before each model call, always keeping the system
   prompt. Without this, `MemorySaver` growth silently overflows a 7B
   context within ~10 turns.
2. **Current-time injection** — `Current time: {now} ({tz})` added to the
   system prompt each turn; small models otherwise hallucinate dates for
   "yesterday"-style history questions.
3. **Audit log** — structured line per tool call (see adapter). This is the
   primary instrument for capability testing and the audit trail for
   iteration-2 writes.
4. **Loop guards** — agent `recursion_limit ≈ 15` + adapter dedupe of
   identical consecutive calls.
5. **Generous timeouts** — explicit values: httpx→Ollama (120 s+), uvicorn
   keep-alive, documented ingress expectations. Old laptop generations can
   exceed 60 s; default timeouts manifest as mysterious 504s.
6. **`/api/health`** — reports HA REST, WS, and Ollama reachability; enables
   the add-on `watchdog`.

## WS client

`ha/websocket.py` (~100 lines): single connection opened in FastAPI lifespan
(or CLI startup); auth handshake (supervisor token in container, long-lived
token in dev); `await ws.request(type, **payload)` with auto-incrementing
ids and futures for correlation; reconnect with backoff. Used for
`config/area_registry/list`, `config/device_registry/list`,
`config/entity_registry/list`, automation config/traces (iteration 1 ships automation config via REST; traces deferred to iteration 2). Registry responses
cached with ~60 s TTL. Event subscriptions are out of scope but the design
does not preclude them.

## Error handling

Adapter-level mapping, never a raw traceback to the model:

| Condition | `error_code` |
|---|---|
| httpx connection/5xx | `ha_unreachable` |
| 404 on entity | `entity_not_found` + `did_you_mean` (fuzzy match against entity registry) |
| pydantic validation | `invalid_params` (message names the bad field) |
| WS request timeout | `ha_timeout` |
| unknown skill | `skill_not_found` + available names |

`did_you_mean` is cheap and disproportionately valuable: small models
frequently misspell entity_ids.

## Security notes

- Read-only in iteration 1 by construction (tier ceiling — write tools are
  not loaded).
- Iteration 2 blast radius bounded by the domain allowlist, enforced in
  both the tool handler and the REST client.
- **Prompt injection**: entity friendly names and logbook text are
  semi-untrusted and enter the prompt. Accepted risk for a private home;
  the allowlist bounds what injected instructions could do. Documented, no
  additional machinery.
- Cloud keys are password-typed addon options; never logged.

## Testing

- **Unit tests** (no HA needed): registry/adapter/tiers, `bound_rows`,
  error mapping, loop guard, config parsing — all with a fake `ToolContext`.
- **WS client**: tested against a recorded/fake handshake server.
- **Eval harness** (`tests/evals/`): YAML cases — prompt → expected tool +
  key params — run against live Ollama, scoring tool-calling reliability
  per model. Formalizes the manual testing currently done in
  `local_agent.py`. With `temperature=0` + fixed `seed`, eval failures are
  regressions, not noise. Requires live Ollama (+ optionally HA); excluded
  from CI.

## Iterations

| | Scope |
|---|---|
| **1** | Registry/adapter/tiers, 8 read tools, skills mechanism, WS client, LLM factory (Ollama + LiteLLM), config, CLI REPL (streaming), robustness items 1–6, unit tests + eval harness. Addon runs with existing UI, `max_tier=1`. |
| **2** | `control_entity` + `trigger_automation`, `call_service()` on REST client, domain allowlist, `max_tier=2` config, write-path audit emphasis, evals for action tools. |
| **3 (later)** | HA Assist integration via a small custom component proxying the conversation API to the addon; HTTP streaming; UI investment. |
