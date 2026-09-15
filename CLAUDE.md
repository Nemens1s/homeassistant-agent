# Agent Gosling — project guide

Home Assistant add-on: a LangChain agent over HA state, powered by a local
Ollama server (cloud models optional via LiteLLM). Iteration 1 (read-only)
is merged; specs and plans for iterations 2 (control + audit) and 3
(Assist + streaming) live in `docs/superpowers/`.

## Commands

- **Python: ALWAYS `uv run python` / `uv pip`** (`uv sync` creates `.venv` at
  repo root with Python 3.14; never system python).
- Tests: `uv run pytest -q` — fast, no HA/Ollama needed.
  Exactly 1 known third-party warning (starlette TestClient deprecation) is
  expected; new warnings are findings.
- Capability REPL: `uv run python -m app.cli` (needs `.env` + live HA + Ollama).
- Tool-selection evals: `uv run python -m tests.evals.run` (needs live
  Ollama; deliberately NOT collected by pytest).
- Server (dev): `uv run uvicorn app.main:create_app --factory --port 8099`.
- HA custom-component tests: `uv sync --group ha && uv run pytest` (needs
  Python >=3.14.2; installs the full HA stack).

## Dev environment

`.env` keys: `HA_BASE_URL`, `HA_TOKEN` (or `SUPERVISOR_TOKEN` — alias),
`LLM_URL` (alias `OLLAMA_URL` still works), `LLM_MODEL`. Beware: the legacy
key `OLLAMA_MODEL` is ignored by `Settings` — only `LLM_MODEL` counts. Config is one pydantic-settings
class in `app/config.py`; in the addon container it reads
`/data/options.json` instead (keys must match Settings field names exactly).

## Architecture (app/)

- `tools/base.py` — `ToolDefinition` (name, description ≤400 chars, pydantic
  params model, `Tier`, async handler), `ToolResult` envelope, `bound_rows`.
- `tools/registry.py` — tools register at import; `_DEFAULT_MODULES` lists
  them; `tools_for_tier(max_tier)` is the permission gate. **Adding a tool =
  one new module + one line in `_DEFAULT_MODULES`.**
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
- `ha/rest.py` — GET-only httpx client (read-only by construction; iteration
  2 adds exactly one allowlist-guarded write method).
- `ha/websocket.py` — persistent WS client (auth, id-correlated `request()`,
  reconnect/backoff, `request_cached` for registries). Tools must tolerate
  `ctx.ws is None` (degraded mode → `ws_unavailable` envelope).
- `agent/llm.py` — `build_llm`: ChatOllama (with num_ctx/num_predict/seed/
  reasoning/keep_alive knobs — small-model tuning is a first-class concern)
  or ChatOpenAI for any OpenAI-compatible cloud endpoint (Baseten, llamacpp,
  etc.) — set LLM_PROVIDER to anything other than "ollama".
- `agent/factory.py` — `build_agent`: langchain v1 `create_agent` + custom
  middleware (per-call history trimming + current-time injection into the
  system prompt; never mutate checkpointed state) + per-run loop-guard reset.
- `skills/` — markdown playbooks with YAML frontmatter, listed in the system
  prompt, loaded on demand via the `load_skill` tool. Skills are data;
  iterate on them without code changes.
- `main.py` — FastAPI app FACTORY (`create_app`; run with `--factory`).
  No import-time singletons anywhere — everything is built by factories
  with injected settings.

## Hard rules

- Read-only guarantee is structural, not a runtime flag: tier gating +
  GET-only REST client (+ WS command allowlist from iteration 2 on).
- Tool output is always `ToolResult...to_json()` compact JSON — never
  `str(dict)`, never raw exceptions.
- langchain v1 API only (`create_agent`, `langchain.agents.middleware`);
  verify API details against the installed venv (`inspect`), don't trust
  training data — this has bitten before.
- Use regular branches. DO not use worktrees
- Prefer plain, readable Python: regular `for` loops over comprehensions,
  explicit steps over clever one-liners. Optimize for readability, not
  brevity.
- Writes are menu-only: the agent's only action tool is `trigger_automation`,
  which triggers only `automation.ai_*` automations, and every ACTION-tier call
  is gated in `tools/adapter.py` by a fail-closed point-read of
  `settings.ai_actions_switch` (default `input_boolean.ai_triggered_actions`).
  Off/unreadable ⇒ refused (`ai_disabled` / `ai_gate_unavailable`).

## Where things live

- Specs: `docs/superpowers/specs/` · Plans: `docs/superpowers/plans/`
  (iterations 2 & 3 are planned but NOT implemented).
- Deployment: `docs/DEPLOYMENT.md` (addon install, standalone docker build
  needs `--build-arg BUILD_FROM=...` — see build.yaml).
- Addon packaging: `config.yaml` (options schema mirrors Settings),
  `run.sh` (bashio, uvicorn --factory), `build.yaml` (base images).
- The mylo project (`/Users/ilniko/IdeaProjects/mylo`) is the heavyweight
  inspiration for the tool registry — reference only, don't copy complexity.
