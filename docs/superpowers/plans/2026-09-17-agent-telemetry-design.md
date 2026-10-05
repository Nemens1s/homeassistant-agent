---
title: Agent telemetry — interaction dataset with OpenTelemetry instrumentation, fast path as middleware
date: 2026-09-17
status: draft (design aligned, not implemented)
supersedes: none
related: 2026-08-25-needle-fast-path-design.md, 2026-09-02-local-ha-agent-iteration-4-design.md
---

# Agent telemetry

## Summary

Instrument Agent Gosling with the **OpenTelemetry (OTel) SDK** and persist every
interaction into a **local, full-fidelity SQLite dataset** in the App's `/data`.
The dataset records queries, replies, per-step thinking, offered vs. chosen
tools, tool results, fast-path decisions, and feedback, together with exact
snapshots of the prompt, tool schemas, and fast-path menu that were in effect.

As a prerequisite, the Needle fast path moves from `main.py` into the agent as
a **`FastPathMiddleware`** with a pluggable backend. A fast-path hit becomes an
ordinary step of the agent run: it is traced like any model call, its tool call
runs through the normal tool pipeline, and the turn is persisted in the
conversation thread.

OTel is used as the **instrumentation API** (spans, parent/child causality,
async context propagation). The first exporter is a custom SQLite exporter. An
OTLP (OpenTelemetry Protocol) exporter to a Collector + Grafana stack on the
ASUS box is a later, optional phase and requires no re-instrumentation.

Nothing leaves the LAN. Telemetry never blocks or fails a request.

## Motivation

Data collection should start now, while memory and event subscriptions are
being built, so there is history to work with when those land. The dataset
serves three goals:

1. **Fast-path fine-tuning data.** If fine-tuning Needle (or a replacement)
   becomes necessary, real utterances with the exact menu the model saw and its
   decision should already exist. Labelling is done separately (manually or by a
   dedicated tool); this spec only guarantees the data and a place for labels.
2. **Tool description refinement.** Small models often reach the right answer
   but overthink or get confused on the way. Thinking, step counts, repeated
   calls, and chosen/offered ratios — per description version — show which
   descriptions cause that.
3. **Judging reactions to events (future).** Once the agent subscribes to HA
   events and uses memory, every decision (including "ignored" and "decided not
   to act") must be traceable and reviewable.

The fast-path refactor is motivated independently:

- **Conversation continuity.** Today a fast-path hit bypasses the agent and is
  never written to the checkpointer, so a follow-up like "turn it off again" has
  no context. As middleware, the hit is persisted like any other turn.
- **One execution path.** The duplicated fast-path branches in both endpoints of
  `main.py` disappear; CLI, UI, and Assist behave identically.
- **Replaceable fast path.** The middleware depends on a backend protocol, not
  on Needle.

## Goals

- `FastPathMiddleware` with a backend protocol; Needle is one implementation.
- Record every request end-to-end, untruncated, with a stable `request_id`.
- Record the **offered** tool set on every model call, not just the chosen tool
  (tool subsetting means "never called" and "never offered" are different
  problems).
- Version every trace by content hashes of the base system prompt, the tool
  schemas, and the fast-path menu.
- Record every fast-path decision (hit or miss) with menu snapshot, prediction,
  confidence, and threshold.
- A generic labels/feedback store keyed by `request_id`, writable from the UI,
  a manual process, or an external labelling tool.
- A metrics tab in the chat UI, served from the local dataset.
- Export commands for fast-path JSONL and `tests/evals/cases.yaml` candidates.
- Fail open: an unwritable DB, a full queue, or an unreachable collector must
  never affect replies or latency.

## Non-goals (v1)

- Automatic labelling of fast-path decisions (no teacher/audit runs).
- Running a candidate fast-path model next to the live one.
- Grafana / Tempo / Prometheus deployment (Phase 3).
- Event-decision tracing (Phase 4 — conventions reserved here).
- Cross-service traces into HA, Whisper, or llama.cpp; traces live inside the
  App process.
- Real-time alerting.

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Fast path placement | `FastPathMiddleware`, short-circuiting `awrap_model_call` | Supported by LangChain 1.x middleware ("skip calling handler to short-circuit"); persists the turn; single path; replaceable |
| Fast path backend | Backend-agnostic protocol `FastPathBackend`; `RemoteNeedleBackend` (and `FakeBackend` for tests) implement it | Swap the model without touching the middleware or telemetry |
| Instrumentation API | OTel SDK spans | Causality and async context propagation; OTLP later without re-instrumenting; learning goal |
| Primary store | SQLite `/data/telemetry.sqlite`, own schema | Full fidelity, indefinite retention, easy export; works when the ASUS box is down |
| Content capture | Full text, no truncation in the dataset | Overthinking is exactly what truncation cuts off |
| Telemetry hook | `TelemetryMiddleware`, directly outside `FastPathMiddleware` | Sees the final model request and both LLM and fast-path responses |
| Labels | Separate table, any source; views derive nothing automatically for fast-path hits | Labelling is a separate concern and tool |
| Separate DB file | Not merged into `audit.db` or `checkpoints.sqlite` | Different retention, backup, and privacy handling |
| Auto-instrumentation libraries | Not used | Domain attributes matter more than generic LLM spans; avoids LangChain 1.x compatibility risk |

## Architecture

### Module layout

```
app/fast_path/
  __init__.py
  middleware.py     # FastPathMiddleware
  backend.py        # FastPathBackend protocol, Decision, FakeBackend
                    # (Decision + FakeBackend move here from app/needle/backend.py;
                    #  the old app/needle NeedleBackend protocol is replaced by this one)
app/needle/         # Needle-specific: RemoteNeedleBackend, MenuProvider, Menu
                    # router.py (FastPathRouter) removed; backend.py protocol removed

app/telemetry/
  __init__.py
  setup.py          # TracerProvider, processors, exporters; init/shutdown from lifespan
  conventions.py    # span names + attribute keys (single source of truth)
  middleware.py     # TelemetryMiddleware (wrap_model_call / wrap_tool_call)
  snapshots.py      # hashing + snapshot registration (prompt, toolset, menu)
  thinking.py       # split_thinking(message) -> (thinking, content); shared with streaming.py
  sqlite_exporter.py# SpanExporter: maps spans to dataset tables
  store.py          # schema, migrations, read queries for the UI/API
  export.py         # CLI: fast-path JSONL, eval case candidates
```

### Middleware stack

Order is outermost first (LangChain composes the first list entry as the outer
layer):

```
ToolSubsetMiddleware          (when enable_tool_subsetting)
ContextWindowMiddleware
LoopGuardResetMiddleware
HistoryCapMiddleware          (when max_history_messages > 0)
TelemetryMiddleware           (when telemetry_enabled)
FastPathMiddleware            (when fast path enabled and trigger tool registered)
```

`TelemetryMiddleware` sits outside `FastPathMiddleware`, so every model step —
LLM or fast path — produces one `chat` span, and it sees the request after tool
subsetting and context trimming.

### Data flow

```
POST /api/chat[/stream]
  └─ span: invoke_agent gosling                 (request root; request_id = trace_id)
       ├─ span: chat <model>          ×N        (TelemetryMiddleware)
       │    └─ span: fast_path.classify         (FastPathMiddleware, first step only)
       └─ span: execute_tool <name>   ×N        (TelemetryMiddleware)

Fast-path hit:   chat needle (synthetic tool call) → execute_tool trigger_automation → chat needle (templated reply)
Fast-path miss:  chat <llm> [→ execute_tool …]* → chat <llm>

TracerProvider
  ├─ BatchSpanProcessor → SqliteSpanExporter   → /data/telemetry.sqlite
  └─ BatchSpanProcessor → OTLPSpanExporter      → Collector on ASUS   (Phase 3, optional)
```

One `BatchSpanProcessor` **per exporter**, so a hung OTLP export can never back
up the SQLite queue.

## `FastPathMiddleware`

### Backend protocol

```python
@dataclass(frozen=True)
class Decision:
    entity_id: str | None     # constrained to the current menu, or None
    confidence: float

class FastPathBackend(Protocol):
    name: str                 # e.g. "needle", "needle-remote"; recorded as the model name
    async def classify(self, message: str, menu: Menu) -> Decision: ...
```

Today there is one concrete backend, `RemoteNeedleBackend` (plus `FakeBackend`
for tests); both already match `classify` and gain a `name`. `FastPathBackend`
replaces the existing `NeedleBackend` protocol in `app/needle/backend.py` — the
middleware depends only on this backend-agnostic protocol, not on Needle. The
menu provider stays backend-agnostic (it lists `automation.ai_action*`).

### Behaviour in `awrap_model_call`

1. **First step of a turn** — the last message in `request.messages` is a
   `HumanMessage`:
   - Skip if the fast path is disabled for this run (see Runtime opt-out).
   - Fetch the menu; if empty, call the handler.
   - `classify` inside a `fast_path.classify` span.
   - If `entity_id` is set and `confidence >= threshold`: return a synthetic
     `AIMessage` with one `trigger_automation` tool call, `id` prefixed
     `fastpath-`, and `response_metadata = {"model_name": backend.name,
     "fast_path": True}`. The handler is not called.
   - Otherwise call the handler (LLM runs as today).
2. **Step after a fast-path tool call** — the last message is a `ToolMessage`
   whose `tool_call_id` starts with `fastpath-`: return a synthetic `AIMessage`
   with the templated reply built from the envelope (existing
   `_reply_from_envelope` logic: success text, error message, or generic
   failure). The handler is not called.
3. **Any other step**: call the handler. The fast path must never intercept a
   step in the middle of an LLM-driven loop.

The synthetic tool call executes through the agent's normal tool node, so the
AI-actions gate, `audit.db`, LoopGuard, and `TelemetryMiddleware.awrap_tool_call`
all apply unchanged. The tool receives the graph's `thread_id` as today.

### Failure handling

- Any exception in menu fetch or `classify` → log, record on the span, call the
  handler. The fast path never fails a request.
- Trigger failure (e.g. AI-actions switch off) → templated error reply, as today.

### Runtime opt-out

A per-run flag (`configurable.fast_path = False`) disables interception. Default
is enabled for chat requests. Future event-driven runs set it explicitly.

### Enablement

The middleware is added only when the fast path is configured **and**
`trigger_automation` is registered (i.e. `max_tier >= 2`). Without the tool, a
synthetic call would reference a non-existent tool.

### Changes outside the middleware

- `main.py`: remove the fast-path branches from `/api/chat` and
  `/api/chat/stream`; remove `app.state.fast_path`. The backend and menu
  provider are built in lifespan and passed to `build_agent`.
- `app/needle/router.py`: removed; `_reply_from_envelope` moves to
  `app/fast_path/middleware.py`.
- `app/needle/backend.py`: the `NeedleBackend` protocol is removed (replaced by
  `FastPathBackend`); `Decision` and `FakeBackend` move to
  `app/fast_path/backend.py`. `RemoteNeedleBackend` stays and gains `name`.
- `app/needle/factory.py`: `build_fast_path_router` becomes a builder that
  returns the backend + menu provider (no `FastPathRouter`), wired into
  `build_agent` in lifespan.
- Replace `print(...)` debugging in `router.py` / `remote_backend.py` with span
  attributes and logging.

### Streaming fix

Short-circuited responses reach `stream_mode="messages"` as complete
`AIMessage` objects, not `AIMessageChunk`. In `app/agent/streaming.py`,
`stream_events` must check `isinstance(msg, AIMessage)` (which also matches
chunks) instead of `isinstance(msg, AIMessageChunk)`. Otherwise both the
fast-path tool call and the reply are dropped and the UI shows an empty answer.

Verified on `langchain==1.3.16` / `langgraph==1.2.11` with a fake model: the
synthetic tool call, tool result, and templated reply are emitted as full
messages; the LLM does not run; thread history contains
`Human → AI(tool_call) → Tool → AI`; a normally streamed LLM response emits only
chunks (no duplicate full `AIMessage`). Confirm the no-duplicate behaviour once
against llama.cpp.

## Telemetry span model

Attribute names follow the OTel GenAI semantic conventions where one exists
(still experimental — pinned in `conventions.py`); project-specific attributes
use the `gosling.` prefix.

**`invoke_agent gosling`** — request root, created in `main.py` (and the CLI)

| Attribute | Notes |
|---|---|
| `gen_ai.operation.name` | `invoke_agent` |
| `gen_ai.conversation.id` | `thread_id` |
| `gen_ai.request.model`, `gosling.llm.provider` | configured LLM |
| `gosling.channel` | `ui` \| `assist` \| `cli` \| `event` (Phase 4) |
| `gosling.endpoint` | `/api/chat`, `/api/chat/stream`, `cli` |
| `gosling.device_id` | optional, forwarded by the Assist component |
| `gosling.input.text` | user message as received (post-Whisper for voice) |
| `gosling.output.text` | final reply |
| `gosling.path` | `fast_path` \| `agent` — `fast_path` if the first step was a hit |
| `gosling.outcome` | `ok` \| `recursion_limit` \| `error` \| `cancelled` |
| `gosling.prompt_hash`, `gosling.toolset_hash` | see Snapshots |
| `gosling.max_tier`, `gosling.app_version` | |
| `gosling.ttft_ms` | streaming endpoint only (from `stream_events`) |

**`chat <model>`** — one per model step (LLM or fast path)

| Attribute | Notes |
|---|---|
| `gen_ai.operation.name` | `chat` |
| `gen_ai.response.model` | from `response_metadata.model_name`; backend name for fast-path steps |
| `gosling.fast_path` | `true` for synthetic fast-path responses |
| `gosling.step` | 1-based index within the request |
| `gosling.tools.offered` | JSON list of tool names in `request.tools` |
| `gosling.messages.count` | messages sent after trimming |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | from `usage_metadata` when the provider returns it |
| `gosling.thinking.text` | full thinking (reasoning field or `<think>` block) |
| `gosling.content.text` | visible content |
| `gosling.tool_calls` | JSON list of `{id, name, args}` |
| `gen_ai.response.finish_reasons` | when available |

**`fast_path.classify`** — child of the first-step `chat` span

| Attribute | Notes |
|---|---|
| `gosling.fast_path.backend` | backend `name` |
| `gosling.fast_path.menu_hash` | content hash (see Snapshots) |
| `gosling.fast_path.entity_id` | prediction or empty |
| `gosling.fast_path.confidence`, `gosling.fast_path.threshold` | |
| `gosling.fast_path.accepted` | hit or miss |
| `gosling.fast_path.skip_reason` | `disabled` \| `empty_menu` \| `error` when not classified |

**`execute_tool <name>`** — one per tool execution

| Attribute | Notes |
|---|---|
| `gen_ai.operation.name` | `execute_tool` |
| `gen_ai.tool.name`, `gen_ai.tool.call.id` | `fastpath-` prefix identifies fast-path calls |
| `gosling.tool.args` | JSON, sorted keys (same form as the adapter/LoopGuard) |
| `gosling.tool.status`, `gosling.tool.error_code` | parsed from the result envelope; `repeated_call` = LoopGuard hit; AI-gate codes as emitted |
| `gosling.tool.result` | full envelope, capped at 64 KB |

The adapter already returns a JSON envelope with `status`/`error_code`; no
changes to `app/tools/adapter.py` are needed.

### Snapshots

Hashes are SHA-256 over canonical JSON, truncated to 16 hex chars. Snapshot
content is written once per hash to its own table.

- **Prompt hash** — over the **base** prompt from `build_system_prompt`, not the
  per-call prompt. `ContextWindowMiddleware` injects the current time on every
  call, which would give every call a unique hash.
- **Toolset hash** — over `[{name, description, args_schema}]` for the full
  registry at build time, sorted by name. The per-call subset is recorded
  separately in `gosling.tools.offered`.
- **Menu hash** — over menu items (`entity_id`, `name`, `description`) exactly as
  sent to the backend. Do not reuse `Menu.signature`: it hashes only the sorted
  entity_id set, so a description edit leaves it unchanged. `signature` stays
  the remote server's compile-cache key.

Prompt and toolset snapshots are registered at agent build time; menu snapshots
on menu refresh.

## Dataset schema

```sql
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE requests (
  request_id      TEXT PRIMARY KEY,   -- trace_id hex
  ts_start        TEXT NOT NULL,      -- UTC ISO-8601
  duration_ms     INTEGER,
  channel         TEXT NOT NULL,
  endpoint        TEXT,
  device_id       TEXT,
  thread_id       TEXT,
  input_text      TEXT NOT NULL,
  output_text     TEXT,
  path            TEXT NOT NULL,      -- fast_path | agent
  outcome         TEXT NOT NULL,
  error           TEXT,
  model           TEXT,
  provider        TEXT,
  app_version     TEXT,
  max_tier        INTEGER,
  prompt_hash     TEXT REFERENCES prompt_snapshots(hash),
  toolset_hash    TEXT REFERENCES toolset_snapshots(hash),
  steps           INTEGER,
  ttft_ms         INTEGER
);

CREATE TABLE model_calls (
  span_id         TEXT PRIMARY KEY,
  request_id      TEXT NOT NULL REFERENCES requests(request_id),
  step            INTEGER NOT NULL,
  ts_start        TEXT NOT NULL,
  duration_ms     INTEGER,
  model           TEXT,
  fast_path       INTEGER NOT NULL DEFAULT 0,
  tools_offered   TEXT NOT NULL,      -- JSON array
  messages_count  INTEGER,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  thinking_text   TEXT,
  content_text    TEXT,
  tool_calls      TEXT,               -- JSON array
  finish_reason   TEXT
);

CREATE TABLE tool_calls (
  span_id         TEXT PRIMARY KEY,
  request_id      TEXT NOT NULL REFERENCES requests(request_id),
  seq             INTEGER NOT NULL,   -- order within request
  tool            TEXT NOT NULL,
  call_id         TEXT,
  args_json       TEXT,
  status          TEXT,
  error_code      TEXT,
  duration_ms     INTEGER,
  result_text     TEXT
);

CREATE TABLE fast_path_decisions (
  request_id      TEXT PRIMARY KEY REFERENCES requests(request_id),
  backend         TEXT NOT NULL,
  menu_hash       TEXT REFERENCES menu_snapshots(hash),
  entity_id       TEXT,
  confidence      REAL,
  threshold       REAL,
  accepted        INTEGER,
  skip_reason     TEXT,
  duration_ms     INTEGER
);

-- Labels and feedback from any source: chat UI, manual review, external labelling tool.
CREATE TABLE labels (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id         TEXT NOT NULL REFERENCES requests(request_id),
  ts                 TEXT NOT NULL,
  source             TEXT NOT NULL,   -- ui | manual | tool:<name>
  rating             INTEGER,         -- -1 | 1 | NULL
  correct_tool       TEXT,
  correct_entity_id  TEXT,            -- "none" = nothing should have been triggered
  note               TEXT
);

CREATE TABLE prompt_snapshots  (hash TEXT PRIMARY KEY, text TEXT NOT NULL,         first_seen TEXT NOT NULL);
CREATE TABLE toolset_snapshots (hash TEXT PRIMARY KEY, schemas_json TEXT NOT NULL, first_seen TEXT NOT NULL);
CREATE TABLE menu_snapshots    (hash TEXT PRIMARY KEY, items_json TEXT NOT NULL,   first_seen TEXT NOT NULL);

CREATE INDEX idx_requests_ts ON requests(ts_start);
CREATE INDEX idx_model_calls_req ON model_calls(request_id);
CREATE INDEX idx_tool_calls_req ON tool_calls(request_id);
CREATE INDEX idx_tool_calls_tool ON tool_calls(tool);
CREATE INDEX idx_labels_req ON labels(request_id);
```

WAL mode, one connection owned by the exporter thread. Migrations are numbered
SQL files applied at startup. External labelling tools write only to `labels`.

### Derived views

**`fast_path_dataset`** — one row per classified request: utterance, backend,
menu snapshot, prediction, confidence, threshold, accepted, latest label (if
any). For misses, also the agent's first `trigger_automation` entity (or `none`)
as a **reference column**, not a label — useful context for a labeller.

**`tool_offer_stats`** — per `toolset_hash`, per tool: times offered, times
chosen, chosen/offered ratio, error rate, `repeated_call` rate. Fast-path steps
excluded.

**`request_quality`** — per request: steps, total thinking chars, thinking chars
before the first tool call, repeated calls, distinct tools used.

Candidate confusion heuristics (views or notebooks, to validate against labels
before trusting):

- **Backtracking** — two different tools called consecutively with the same
  `entity_id` argument.
- **Overthinking** — thinking chars before first tool call above the per-model
  p90.
- **Retry loops** — any `repeated_call` error in the request.

## Components

### `TelemetryMiddleware`

- `awrap_model_call(request, handler)`: start `chat` span, set offered tools and
  message count, `await handler(request)`, then read the returned message(s):
  model name and fast-path flag from `response_metadata`, thinking/content via
  `thinking.split_thinking`, token usage, tool calls. End span. Sync
  `wrap_model_call` mirrors it for the CLI.
- `awrap_tool_call(request, handler)`: start `execute_tool` span, run handler,
  parse the result envelope for status/error_code, end span.
- Step counter lives in a contextvar set by the request span, not on the
  middleware instance (the agent is shared across concurrent requests).
- Every attribute extraction is wrapped: an exception in telemetry code is
  logged and swallowed, and the handler result is always returned.

### `thinking.split_thinking`

Non-streaming counterpart of `_ThinkBuffer`: reads
`additional_kwargs["reasoning_content"]` (Ollama), falls back to extracting
`<think>…</think>` from content (llama.cpp). `streaming.py` keeps its buffer;
both share the tag constants. Thinking is only captured when the model under
test emits reasoning.

### Request span in `main.py`

- Both endpoints open the root span around the agent invocation and set `path`,
  `outcome`, and `output_text` before closing. `path` is read from the first
  `chat` span's fast-path flag via a contextvar.
- Streaming: the span is ended in a `finally` inside the SSE generator. Client
  disconnect → `outcome=cancelled`. TTFT is measured on the first `token` event.
- `request_id` is returned to clients: new field on `ChatResponse`, and on the
  SSE `done` and `error` events. The Assist component reads only `reply`, so the
  field is safe.
- `channel`: derived from the endpoint — `/api/chat` → `assist`,
  `/api/chat/stream` → `ui`, CLI → `cli`. An optional `channel` field on
  `ChatRequest` overrides it. `ChatRequest` ignores unknown fields, so the
  component and App can be deployed in either order.

### Labels API and UI

- `POST /api/labels` — `{request_id, source, rating?, correct_tool?, correct_entity_id?, note?}`.
- Chat UI: thumbs up/down under each reply (`source=ui`); on thumbs down, an
  optional "should have been" picker (tools, menu automations, "none").
- External labelling tools use the same endpoint or write to `labels` directly.

### Metrics tab

- `GET /api/telemetry/summary?days=7` — aggregates from the store.
- `GET /api/telemetry/requests?limit=&cursor=` — recent requests with drill-down
  into steps, thinking, and tool calls.
- Frontend: new tab in `frontend/`, vanilla JS. Charts drawn with plain SVG/CSS
  or a vendored library file — no CDN.

First-version panels:

- Requests per day, split by `path`
- Duration p50/p95 and TTFT p50/p95, split by `path`
- Tool table: offered, chosen, chosen/offered, error rate (current toolset hash)
- Outcome rates: recursion limit, errors, LoopGuard hits
- Fast path: hit rate, confidence histogram (hits vs. misses), labelled share
- Labels: counts by source, list of thumbs-down requests

### Export CLI

```
uv run python -m app.telemetry.export fast-path [--labelled-only] --out fast_path.jsonl
uv run python -m app.telemetry.export evals --labelled-only --out candidates.yaml
```

- `fast-path`: one line per classified request —
  `{request_id, utterance, menu: [...], prediction, confidence, accepted, label?, agent_reference?}`
  with the menu resolved from `menu_snapshots`.
- `evals`: entries in the existing `tests/evals/cases.yaml` format (`id`,
  `prompt`, `expect_tool`, `expect_params`) from labelled requests, for manual
  curation. Production data proposes cases; description changes are judged
  offline on the curated set, not by comparing production weeks.

## Configuration

| Option | Default | Notes |
|---|---|---|
| `telemetry_enabled` | `true` | `false` = no provider, no-op tracer, middleware not added |
| `telemetry_db_path` | `/data/telemetry.sqlite` | empty = SQLite exporter disabled |
| `telemetry_retention_days` | `0` | `0` = keep forever; otherwise nightly prune |
| `otlp_endpoint` | `""` | Phase 3; empty = disabled; must be a LAN address |

Existing `needle_*` options keep their meaning and now configure the
`FastPathMiddleware` with the Needle backend.

New dependencies: `opentelemetry-api`, `opentelemetry-sdk`. Phase 3 adds
`opentelemetry-exporter-otlp-proto-http` (HTTP avoids the `grpcio` dependency).

### Attribute length limits

Do **not** set `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` or SDK span limits. They
apply at span creation and would truncate the dataset as well. Truncation for
Grafana happens in the Collector (`transform` processor) in Phase 3.

## Privacy and locality

- No exporters other than SQLite and an explicit LAN OTLP endpoint. Validate at
  startup that `otlp_endpoint` resolves to a private address; refuse otherwise.
- Ensure `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2` are unset in the image.
- The dataset contains presence-related tool results (`get_person_locations`)
  and household activity. Add-on `/data` is included in HA backups; if backups
  go off-site (e.g. Home Assistant Cloud), exclude the file via `backup_exclude`
  in `config.yaml` and back it up separately to the ASUS box. Verify the exclude
  pattern against current add-on docs.
- Phase 3 Grafana: bind to LAN only; `GF_ANALYTICS_REPORTING_ENABLED=false`,
  `GF_ANALYTICS_CHECK_FOR_UPDATES=false`.

## Error handling & degradation (fail open)

| Failure | Behaviour |
|---|---|
| Fast-path menu/backend error | Logged, recorded on span, handler called — LLM handles the request |
| Fast-path trigger fails | Templated error reply (as today) |
| DB path unwritable / migration fails | Log once, disable SQLite exporter, agent runs normally |
| Exporter queue full | Spans dropped by `BatchSpanProcessor`; count drops and log periodically |
| OTLP endpoint down | Only the OTLP processor drops; SQLite unaffected |
| Exception in telemetry attribute code | Logged and swallowed; handler result returned unchanged |
| Shutdown | `TracerProvider.shutdown()` in lifespan teardown flushes queues (bounded timeout) |

## Testing

Fast path:

- **Hit**: `FakeBackend` above threshold → history is
  `Human → AI(tool_call fastpath-*) → Tool → AI(templated)`; LLM fake asserts not called.
- **Miss**: below threshold → LLM called, no synthetic messages.
- **No mid-loop interception**: an LLM-driven turn with tool results never
  triggers classification after step 1.
- **Follow-up context**: a second turn in the same thread sees the fast-path turn
  in history.
- **Gate and audit**: fast-path trigger with the AI-actions switch off returns
  the gate error; tier-2 audit row written on success.
- **Backend error** → falls through to LLM.
- **Opt-out flag** → no classification.
- **Not added** when `trigger_automation` is not registered.
- **Streaming**: `/api/chat/stream` emits the fast-path reply as `token` and
  `done` with the `isinstance(msg, AIMessage)` check; an LLM streamed reply is
  not duplicated.

Telemetry:

- **Exporter mapping**: spans from `InMemorySpanExporter` fixtures → rows per
  table in a temp DB.
- **Middleware ordering**: with subsetting enabled, `model_calls.tools_offered`
  equals the subset.
- **Fast-path spans**: hit produces two `chat` spans flagged `fast_path`, one
  `fast_path.classify`, one `execute_tool`; `requests.path = fast_path`.
- **Context propagation**: all spans share the request's trace_id under
  concurrent requests.
- **Thinking split**: reasoning-field and inline-`<think>` inputs.
- **Prompt hash stability**: two calls in one request (different injected time)
  produce the same `prompt_hash`.
- **Menu hash**: description change → new `menu_hash`, same `Menu.signature`.
- **Fail open**: read-only DB path and a raising exporter → `/api/chat` still
  replies.
- **Streaming cancel**: disconnect mid-stream → `outcome=cancelled`.
- **Export**: `evals` output parses with `tests/evals/run.py`'s loader.

## Rollout

1. **Phase 0a — Fast path as middleware.** `app/fast_path/`, backend protocol,
   middleware, removal of fast-path branches in `main.py`, streaming
   `isinstance` fix. Behaviour-preserving apart from persisted history.
2. **Phase 0b — Dataset.** `app/telemetry/` package, SQLite exporter, schema,
   `TelemetryMiddleware`, request spans, snapshots, `request_id` in responses.
   Deploy and start collecting.
3. **Phase 1 — Labels & UI.** Labels endpoint + thumbs, metrics tab, export CLI,
   views.
4. **Phase 2 — Operational stack (optional, learning).** OTLP exporter;
   docker-compose on the ASUS box: Collector (`spanmetrics` connector for
   request/error/duration metrics, `transform` for truncation), Tempo,
   Prometheus, Grafana, with memory limits and persistent volumes so it moves to
   the Framework box unchanged.
5. **Phase 3 — Event decisions.** Built with memory/event subscriptions.

## Event decisions — conventions (reserved)

Root span `handle_event gosling` with `gosling.channel=event` and the fast path
opted out unless explicitly wanted. When the event loop is built:

- Record **filtered-out** events too (sampled or aggregated if high-volume), with
  the reason — a missed reaction starts as a filter decision.
- "Decided not to act" is an explicit outcome with its thinking, never an absent
  row.
- Record IDs/versions of memory entries injected into context.
- Actions get a linked `verify_effect` span a few seconds later, checking the
  intended state change actually happened.

```sql
CREATE TABLE event_decisions (
  request_id        TEXT PRIMARY KEY,
  ts                TEXT NOT NULL,
  entity_id         TEXT NOT NULL,
  old_state         TEXT,
  new_state         TEXT,
  forwarded         INTEGER NOT NULL,   -- passed the watchlist filter
  filter_reason     TEXT,
  decision          TEXT,               -- act | no_action | NULL if not forwarded
  memory_refs       TEXT,               -- JSON
  verified          INTEGER             -- NULL until verify_effect runs
);
```

Review labels for event decisions go into `labels`.

## Risks & open questions

- **TTFT on non-streaming calls.** The middleware sees complete messages only;
  TTFT is recorded for the streaming endpoint only.
- **Token usage availability.** Depends on the provider (`llamacpp` path via
  `reasoning_chat_openai.py`) returning usage; fields stay NULL otherwise.
- **Synthetic messages in history.** The LLM will see fast-path tool calls it
  did not produce in later turns. Expected to be harmless (standard message
  format); watch for confusion in follow-ups.
- **Fast-path reply on trigger failure.** Templated for speed; handing that step
  to the LLM for a better explanation is a possible later option.
- **Semantic convention churn.** GenAI conventions are experimental; names are
  pinned in `conventions.py`.
- **DB growth.** Estimated a few MB/day with full thinking; revisit if tool
  results (e.g. `get_history`) dominate — the 64 KB cap is the lever.
- **llama.cpp streaming duplication.** Verified no duplicate full `AIMessage`
  with a fake streaming model; confirm once against the real provider after the
  `isinstance` change.
