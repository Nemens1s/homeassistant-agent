# Agent Gosling — project guide

Home Assistant add-on: a LangChain agent over HA state, powered by a local
llama.cpp server (any OpenAI-compatible cloud endpoint optional).
Read tools, menu-only actions + audit, streaming chat, event notes, the
language layer and degraded (command) mode are all implemented; their specs
and plans live in `docs/superpowers/`.

## Commands

- **Python: ALWAYS `uv run python` / `uv pip`** (`uv sync` creates `.venv` at
  repo root with Python 3.14, pinned in `.python-version` — the add-on image
  ships the same version; never system python).
- Tests: `uv run pytest -q` — fast, no HA/LLM needed; CI runs it on
  every PR (`.github/workflows/tests.yml`).
  The run is warning-free; any new warning is a finding.
- Capability REPL: `uv run python -m app.cli` (needs `.env` + live HA + LLM server).
- Tool-selection evals: `uv run python -m tests.evals.run` (needs a live
  LLM server; deliberately NOT collected by pytest).
- Eval results viewer: `uv run python -m tests.evals.view` (read-only page
  over `.evals/`; compare up to 3 runs).
- Server (dev): `uv run uvicorn app.main:create_app --factory --port 8099`.

## Dev environment

`.env` keys: `HA_BASE_URL`, `HA_TOKEN` (or `SUPERVISOR_TOKEN` — alias),
`LLM_URL`, `LLM_MODEL`. Config is one pydantic-settings
class in `app/config.py`; in the addon container it reads
`/data/options.json` instead (keys must match Settings field names exactly).

## Architecture (app/)

- `tools/base.py` — `ToolDefinition` (name, description ≤400 chars, pydantic
  params model, `Tier`, async handler), `ToolResult` envelope, `bound_rows`.
- `tools/registry.py` — tools register at import; `_DEFAULT_MODULES` lists
  them; `tools_for_tier(max_tier)` is the permission gate; `resolve_enabled(ctx)`
  additionally drops tools disabled by name or whose `available=` probe says
  there's nothing in this home to use them on. **Adding a tool = one new
  module + one line in `_DEFAULT_MODULES`** (plus an `available=` probe on the
  `ToolDefinition` when the tool only makes sense if something backs it,
  e.g. a matching entity or `settings.skills_enabled`).
- `tools/adapter.py` — THE seam. Converts definitions to LangChain
  `StructuredTool`s and owns ALL cross-cutting behavior: exception→envelope
  mapping, audit log (`agent.tools` logger, `tool=... status=... duration_ms=...`
  format — tests assert substrings), loop guard, compact JSON. **Handlers
  never raise into the agent loop; never log/handle errors inside handlers.**
- `tools/context.py` — `ToolContext` carries rest/ws clients + settings;
  handlers never import clients (tests pass fakes).
- `tools/helpers/` — reusable utilities shared across tool modules (register
  nothing): `timerange.py` (range parsing),
  `lookups.py` (area/device/entity resolution over HA registries — e.g.
  `resolve_area`, `entity_area_ids`, `device_name`). Not `tools/registry.py`,
  which is the tool catalogue.
- `ha/rest.py` — httpx client: GETs plus exactly one write method,
  `call_service`, which refuses any domain outside the allowlist it was
  built with (`WRITE_DOMAINS` only when `allow_actions` is on, else empty).
- `ha/websocket.py` — persistent WS client (auth, id-correlated `request()`,
  reconnect/backoff, `request_cached` for registries). Tools must tolerate
  `ctx.ws is None` (degraded mode → `ws_unavailable` envelope).
- `agent/llm.py` — `build_llm`: ChatOpenAI (`ReasoningChatOpenAI`) for every
  backend. `llamacpp` adds min_p/top_k/repeat_penalty via `extra_body`
  (small-model tuning is a first-class concern); any other provider gets only
  OpenAI-spec params, safe for strict cloud endpoints.
- `health.py` — `ServiceHealth`: generic up/down tracker for one HTTP
  dependency (probe loop 15 s down / 60 s up + passive `mark_down`). Two
  instances on `app.state`: `llm_health`, `fp_health` (fast path, optional).
- `agent/llm_health.py` — provider-specific probes, `is_llm_outage(exc)`
  (connection/timeout/5xx = outage; bad output is not), and re-resolving an
  `auto` provider that could not be identified at boot (agent hot-swap).
- `agent/factory.py` — `build_agent`: langchain v1 `create_agent` + custom
  middleware (per-call history trimming + the current time appended to the
  latest user turn; never mutate checkpointed state) + per-run loop-guard
  reset. The system prompt must stay byte-identical between calls — anything
  per-call goes at the END of the prompt, or llama.cpp's prefix cache misses.
- `i18n/` — the language adapter at the HTTP edge (off by default,
  `language_layer_enabled`). `inbound()` translates the user's message to
  English before the agent runs, `outbound()` translates the reply back, via
  the `lang-mt` service; `glossary.py` masks entity names as `[E1]`
  placeholders. The agent core — graph, prompts, tools, checkpointed messages —
  never sees non-English text, and every lang-mt failure fails open (the user
  gets an English answer). `store.py` keeps a `lang_overlay` table in the
  checkpoint DB so `/api/history` can show native text after a reload. Spec:
  `docs/superpowers/specs/2026-09-29-multilingual-language-layer-design.md`.
- `skills/` — markdown playbook parser (`list_skills`/`read_skill`). Skills
  are user-supplied and off by default (`settings.skills_enabled`, default
  `False`): nothing ships in `app/skills/` — users drop their own files into
  `settings.skills_path` (default `/config/gosling/skills`); see
  `docs/examples/skills/` for the format. When enabled, the system prompt
  lists name+description one-liners and `load_skill` loads one on demand;
  `load_skill`'s `available=` probe keeps it off the tool menu unless skills
  are enabled and at least one file parses.
- `events/` — the proactive half. `listener.py` holds one `subscribe_trigger`
  over `watched_entities` + `clock_entity`, matches each event against notes
  in SQL, and only then hands them to `runner.py`: a task with a saved
  action/reminder runs directly through the trigger_action/notify_user tools
  (no LLM); only a task with neither falls back to an agent run (thread
  `events`, fast path off). Every fired task records an outcome. `clock.py` parses the World Clock sensor (`HH:MM DD-MM-YYYY`);
  all time-note maths stays in that wall time. Most events cost no LLM call.
- `degraded/` — `CommandRouter` at the HTTP edge: slash commands always,
  free text only while the LLM is down (deferral guard → fast path → fuzzy
  action matcher → "offline" reply). Runs tools via `to_structured_tool`
  (gate + audit apply). Replies are English and never checkpointed.
- `memory/store.py` — `NoteStore`, the `memory_notes` table in the checkpoint
  DB. One-shot notes (state or time trigger), English instruction + original
  text, never raises.
- `agent/run_scope.py` — per-run facts the agent never sees (user language,
  notify suppression, thread id). Callers put them in `configurable`; the
  adapter exposes them to handlers via `current_scope()`.
- `main.py` — FastAPI app FACTORY (`create_app`; run with `--factory`).
  Holds only the lifespan (builds everything onto `app.state`) and router
  wiring. No import-time singletons anywhere — everything is built by
  factories with injected settings.
- `api/` — the HTTP routers, reading dependencies from `request.app.state`:
  `chat.py` (`/api/chat` + `/api/chat/stream`; `ChatTurn` holds the shared
  routing/translation/outage sequence), `system.py` (`/api/health` — always
  HTTP 200, see the comment there — `/api/prompt`, `/api/history`),
  `actions.py`, `notes.py`, `telemetry.py`.
- `retention.py` — daily pruning: unlabelled telemetry requests older than
  `telemetry_retention_days`, and threads whose newest checkpoint is older
  than `thread_retention_days` (0 = keep). Thread activity is read from the
  checkpoints table, so no writer has to report in.

## Hard rules

- Read-only guarantee is structural, not a runtime flag: tier gating + the
  REST client's write-domain allowlist (empty unless `allow_actions`) + the
  WS command allowlists.
- Tool output is always `ToolResult...to_json()` compact JSON — never
  `str(dict)`, never raw exceptions.
- langchain v1 API only (`create_agent`, `langchain.agents.middleware`);
  verify API details against the installed venv (`inspect`), don't trust
  training data — this has bitten before.
- Use regular branches. DO not use worktrees
- No direct commits or pushes to `main` — they are rejected by branch protection. All changes go through a feature branch and a PR.
- Prefer plain, readable Python: regular `for` loops over comprehensions,
  explicit steps over clever one-liners. Optimize for readability, not
  brevity.
- Writes are menu-only: the agent's action tools are `trigger_action` (only
  `automation.ai_*` / `script.ai_*`) and `notify_user` (only
  `settings.notify_action`, itself a `script.ai_*`; it translates, and may
  silently suppress when event confirmations are off). Every ACTION-tier call
  is gated in `tools/adapter.py` by a fail-closed point-read of
  `settings.ai_actions_switch` (default `input_boolean.ai_triggered_actions`).
  Off/unreadable ⇒ refused (`ai_disabled` / `ai_gate_unavailable`). The memory
  note tools are READ tier on purpose: they write agent-local state, never HA.
- HA scripts live in the SmartHome repo (`Suur-Ameerika/ai_actions/`); this
  repo only defines their contract.

## Where things live

- Specs: `docs/superpowers/specs/` · Plans: `docs/superpowers/plans/`.
- Deployment: `docs/DEPLOYMENT.md` (addon install, standalone docker build;
  `--build-arg BUILD_ARCH=aarch64` for ARM).
- Addon packaging: `config.yaml` (options schema mirrors Settings),
  `run.sh` (bashio, uvicorn --factory), `Dockerfile` (Debian base, Python
  installed by uv from `.python-version`). Version: `pyproject.toml` is the
  source; the post-merge bump workflow writes it to `config.yaml` too.
