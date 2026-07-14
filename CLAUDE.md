# Local HA Agent — project guide

Home Assistant add-on: a LangChain agent over HA state, powered by a local
Ollama server (cloud models optional via LiteLLM). Iteration 1 (read-only)
is merged; specs and plans for iterations 2 (control + audit) and 3
(Assist + streaming) live in `docs/superpowers/`.

## Commands

- **Python: ALWAYS `venv/bin/python` / `venv/bin/pip`** (Python 3.14 venv at
  repo root; never system python).
- Tests: `venv/bin/python -m pytest -q` — fast, no HA/Ollama needed.
  Exactly 2 known third-party warnings (langchain pydantic-v1 shim,
  starlette TestClient deprecation) are expected; new warnings are findings.
- Capability REPL: `venv/bin/python -m app.cli` (needs `.env` + live HA + Ollama).
- Tool-selection evals: `venv/bin/python -m tests.evals.run` (needs live
  Ollama; deliberately NOT collected by pytest). Baseline: 8/9 on qwen3.5:4b.
- Server (dev): `venv/bin/uvicorn app.main:create_app --factory --port 8099`.

## Dev environment

`.env` keys: `HA_BASE_URL`, `HA_TOKEN` (or `SUPERVISOR_TOKEN` — alias),
`OLLAMA_URL`, `LLM_MODEL`. Beware: the legacy key `OLLAMA_MODEL` is ignored
by `Settings` — only `LLM_MODEL` counts. Config is one pydantic-settings
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
- `ha/rest.py` — GET-only httpx client (read-only by construction; iteration
  2 adds exactly one allowlist-guarded write method).
- `ha/websocket.py` — persistent WS client (auth, id-correlated `request()`,
  reconnect/backoff, `request_cached` for registries). Tools must tolerate
  `ctx.ws is None` (degraded mode → `ws_unavailable` envelope).
- `agent/llm.py` — `build_llm`: ChatOllama (with num_ctx/num_predict/seed/
  reasoning/keep_alive knobs — small-model tuning is a first-class concern)
  or ChatLiteLLM for cloud.
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
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Where things live

- Specs: `docs/superpowers/specs/` · Plans: `docs/superpowers/plans/`
  (iterations 2 & 3 are planned but NOT implemented).
- Deployment: `docs/DEPLOYMENT.md` (addon install, standalone docker build
  needs `--build-arg BUILD_FROM=...` — see build.yaml).
- Addon packaging: `config.yaml` (options schema mirrors Settings),
  `run.sh` (bashio, uvicorn --factory), `build.yaml` (base images).
- The mylo project (`/Users/ilniko/IdeaProjects/mylo`) is the heavyweight
  inspiration for the tool registry — reference only, don't copy complexity.
