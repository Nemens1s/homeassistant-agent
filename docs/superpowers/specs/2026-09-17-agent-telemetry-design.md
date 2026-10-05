---
title: Agent telemetry — full-fidelity interaction dataset with OTel, fast path as middleware
date: 2026-09-17
status: draft (design approved, not implemented)
supersedes: none
related: ../plans/2026-09-17-agent-telemetry-design.md (detailed design), 2026-08-25-needle-fast-path-design.md, 2026-09-02-local-ha-agent-iteration-4-design.md
---

# Agent telemetry

## Summary

Instrument Agent Gosling with the **OpenTelemetry (OTel) SDK** and persist every
interaction into a **local, full-fidelity SQLite dataset** in the add-on's
`/data`. The dataset records queries, replies, per-step thinking, offered vs.
chosen tools, tool results, fast-path decisions, and feedback — together with
exact snapshots of the prompt, tool schemas, and fast-path menu that were in
effect for each request.

As a prerequisite, the Needle fast path moves out of `main.py` and into the
agent as a **`FastPathMiddleware`** with a backend-agnostic protocol. A fast-path
hit becomes an ordinary step of the agent run: it is traced like any model call,
its tool call runs through the normal tool pipeline (gate + audit + loop guard),
and the turn is persisted in the conversation thread.

OTel is the **instrumentation API** (spans, parent/child causality, async
context propagation). The first and only v1 exporter is a custom SQLite
exporter. An OTLP exporter to a Collector + Grafana stack is a later, optional
phase and requires no re-instrumentation.

Nothing leaves the LAN. Telemetry never blocks or fails a request.

## Motivation

Data collection should start now, while memory and event subscriptions are being
built, so there is history to work with when those land. The dataset serves
three goals:

1. **Fast-path fine-tuning data.** If fine-tuning Needle (or a replacement)
   becomes necessary, real utterances with the exact menu the model saw and its
   decision should already exist. Labelling is a separate concern (manual or a
   dedicated tool); this spec only guarantees the data and a place for labels.
2. **Tool-description refinement.** Small models often reach the right answer but
   overthink or get confused on the way. Thinking, step counts, repeated calls,
   and chosen/offered ratios — per description version — show which descriptions
   cause that.
3. **Judging reactions to events (future).** Once the agent subscribes to HA
   events and uses memory, every decision (including "ignored" and "decided not
   to act") must be traceable and reviewable.

The fast-path refactor is motivated independently:

- **Conversation continuity.** Today a fast-path hit bypasses the agent and is
  never written to the checkpointer, so a follow-up like "turn it off again" has
  no context. As middleware, the hit is persisted like any other turn.
- **One execution path.** The duplicated fast-path branches in both endpoints of
  `main.py` disappear; CLI, UI, and Assist behave identically.
- **Replaceable fast path.** The middleware depends on a backend protocol, not on
  Needle specifically.

## Goals

- `FastPathMiddleware` with a backend-agnostic protocol; the Needle remote
  backend is one implementation.
- Record every request end-to-end, untruncated, with a stable `request_id`.
- Record the **offered** tool set on every model call, not just the chosen tool
  (tool subsetting means "never called" and "never offered" are different
  problems).
- Version every trace by content hashes of the base system prompt, the tool
  schemas, and the fast-path menu.
- Record every fast-path decision (hit or miss) with menu snapshot, prediction,
  confidence, and threshold.
- A generic labels/feedback store keyed by `request_id`, writable from the UI, a
  manual process, or an external labelling tool.
- A metrics tab in the chat UI, served from the local dataset.
- Export commands for fast-path JSONL and `tests/evals/cases.yaml` candidates.
- Fail open: an unwritable DB, a full queue, or an unreachable collector must
  never affect replies or latency.

## Non-goals (v1)

v1 is **Phases 0a + 0b + 1** (fast path as middleware, the dataset, then
labels/UI/export — see Rollout). Explicitly out of scope:

- Automatic labelling of fast-path decisions (no teacher/audit runs).
- Running a candidate fast-path model next to the live one.
- **Grafana / OTLP / Tempo / Prometheus deployment (Phase 2, reserved).** The
  span model is designed so this needs no re-instrumentation.
- **Event-decision tracing (Phase 3, reserved).** Conventions and a reserved
  `event_decisions` table shape are recorded in the detailed design doc.
- Cross-service traces into HA, Whisper, or llama.cpp — traces live inside the
  add-on process.
- Real-time alerting.

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Fast path placement | `FastPathMiddleware`, short-circuiting `awrap_model_call` | LangChain 1.x middleware supports "skip calling handler to short-circuit"; persists the turn; single path; replaceable |
| Fast path backend | Backend-agnostic protocol `FastPathBackend`; `RemoteNeedleBackend` (and `FakeBackend` for tests) implement it | Swap the model without touching the middleware or telemetry; replaces the old `NeedleBackend` protocol |
| Instrumentation API | OTel SDK spans | Causality + async context propagation; OTLP later without re-instrumenting |
| Primary store | SQLite `/data/telemetry.sqlite`, own schema | Full fidelity, indefinite retention, easy export; works when any external box is down |
| Content capture | Full text, no truncation in the dataset | Overthinking is exactly what truncation cuts off |
| Telemetry hook | `TelemetryMiddleware`, directly outside `FastPathMiddleware` | Sees the final (post-subset, post-trim) model request and both LLM and fast-path responses |
| Labels | Separate table, any source; views derive nothing automatically for fast-path hits | Labelling is a separate concern and tool |
| Separate DB file | Not merged into `audit.db` or `checkpoints.sqlite` | Different retention, backup, and privacy handling |
| Auto-instrumentation libs | Not used | Domain attributes matter more than generic LLM spans; avoids LangChain 1.x compatibility risk |
| One `BatchSpanProcessor` per exporter | SQLite and (later) OTLP each get their own | A hung OTLP export can never back up the SQLite queue |

## Architecture

### Module layout

```
app/fast_path/
  middleware.py     # FastPathMiddleware
  backend.py        # FastPathBackend protocol, Decision, FakeBackend
app/needle/         # RemoteNeedleBackend, MenuProvider, Menu
                    # router.py (FastPathRouter) removed; old NeedleBackend protocol removed

app/telemetry/
  setup.py          # TracerProvider, processors, exporters; init/shutdown from lifespan
  conventions.py    # span names + attribute keys (single source of truth)
  middleware.py     # TelemetryMiddleware (wrap_model_call / wrap_tool_call)
  snapshots.py      # hashing + snapshot registration (prompt, toolset, menu)
  thinking.py       # split_thinking(message) -> (thinking, content); shared with streaming.py
  sqlite_exporter.py# SpanExporter: maps spans to dataset tables
  store.py          # schema, migrations, read queries for the UI/API
  export.py         # CLI: fast-path JSONL, eval-case candidates
```

`Decision` and `FakeBackend` move from `app/needle/backend.py` into
`app/fast_path/backend.py`; the old `app/needle` `NeedleBackend` protocol is
replaced by `FastPathBackend`. `RemoteNeedleBackend` stays and gains a `name`
attribute. `app/needle/factory.py` becomes a builder that returns the backend +
menu provider (not a `FastPathRouter`), wired into `build_agent` in the lifespan.

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

`TelemetryMiddleware` sits **outside** `FastPathMiddleware`, so every model step
— LLM or fast path — produces one `chat` span, and it sees the request after
tool subsetting and context trimming.

### `FastPathMiddleware`

Backend protocol:

```python
@dataclass(frozen=True)
class Decision:
    entity_id: str | None     # constrained to the current menu, or None
    confidence: float

class FastPathBackend(Protocol):
    name: str                 # e.g. "needle-remote"; recorded as the model name
    async def classify(self, message: str, menu: Menu) -> Decision: ...
```

`awrap_model_call` dispatches on the last message in `request.messages`:

1. **First step of a turn** (last message is a `HumanMessage`): fetch the menu;
   if empty or the fast path is opted out for this run, call the handler.
   Otherwise `classify` inside a `fast_path.classify` span. If `entity_id` is set
   and `confidence >= threshold`, return a synthetic `AIMessage` with one
   `trigger_automation` tool call, `id` prefixed `fastpath-`, and
   `response_metadata = {"model_name": backend.name, "fast_path": True}` — the
   handler is not called. Otherwise call the handler (LLM runs as today).
   Note: with fine-tuned Needle weights the backend reports `confidence` as
   `None` → coerced to `0.0`, so the default `needle_confidence_threshold = 0.0`
   ("trust the grammar-constrained call") is intentional; a nonzero threshold is
   for the calibrated base model only.
2. **Step after a fast-path tool call** (last message is a `ToolMessage` whose
   `tool_call_id` starts with `fastpath-`): return a synthetic `AIMessage` with
   the templated reply built from the tool envelope (the existing
   `_reply_from_envelope` logic, moved into this module). Handler not called.
3. **Any other step**: call the handler. The fast path must never intercept a
   step in the middle of an LLM-driven loop.

The synthetic tool call executes through the agent's normal tool node, so the
AI-actions gate, `audit.db`, the loop guard, and `TelemetryMiddleware`'s tool
wrapping all apply unchanged, and the tool receives the graph's `thread_id`.

**Fail open.** Any exception in menu fetch or `classify` is logged, recorded on
the span, and the handler is called (LLM handles the request). A trigger failure
(e.g. AI-actions switch off) returns the templated error reply, as today.

**Runtime opt-out.** A per-run flag (`configurable.fast_path = False`) disables
interception; default enabled for chat. Future event-driven runs set it
explicitly.

**Enablement.** The middleware is added only when the fast path is configured
**and** `trigger_automation` is registered (`max_tier >= 2`); without the tool a
synthetic call would reference a non-existent tool.

**Changes outside the middleware.** `main.py` loses the fast-path branches in
`/api/chat` and `/api/chat/stream` and `app.state.fast_path`; the backend and
menu provider are built in the lifespan and passed to `build_agent`.
`app/needle/router.py` is removed. `print(...)` debugging in the Needle modules
is replaced with span attributes and logging.

**Streaming fix.** Short-circuited responses reach `stream_mode="messages"` as
complete `AIMessage` objects, not `AIMessageChunk`. `app/agent/streaming.py`'s
`stream_events` must check `isinstance(msg, AIMessage)` (which also matches
chunks) instead of `AIMessageChunk`; otherwise the fast-path tool call and reply
are dropped and the UI shows an empty answer. Verified on
`langchain==1.3.16` / `langgraph==1.2.11` with a fake model that a normally
streamed LLM response is not re-emitted as a full `AIMessage` (no duplication);
confirm once against llama.cpp.

### Telemetry span model

Attribute names follow the OTel GenAI semantic conventions where one exists
(still experimental — **pinned in `conventions.py`**); project-specific
attributes use the `gosling.` prefix. Four span types:

- **`invoke_agent gosling`** — request root, created in `main.py` (and the CLI).
  `request_id = trace_id`. Carries channel/endpoint/device, input & final output
  text, `path` (`fast_path` | `agent`), `outcome` (`ok` | `recursion_limit` |
  `error` | `cancelled`), the prompt/toolset hashes, model/provider, `max_tier`,
  `app_version`, and (streaming only) TTFT.
- **`chat <model>`** — one per model step (LLM or fast path). Carries the
  response model (backend name for fast-path steps), a `fast_path` flag, 1-based
  step index, offered tool names, message count after trimming, token usage when
  the provider returns it, full thinking and visible content, tool calls, and
  finish reason.
- **`fast_path.classify`** — child of the first-step `chat` span. Carries backend
  name, menu hash, predicted entity, confidence, threshold, accepted flag, and a
  `skip_reason` (`disabled` | `empty_menu` | `error`) when not classified.
- **`execute_tool <name>`** — one per tool execution. Carries tool name and call
  id (the `fastpath-` prefix identifies fast-path calls), JSON args (sorted keys,
  same form as the adapter/loop guard), status and error code parsed from the
  result envelope (`repeated_call` = loop-guard hit; AI-gate codes as emitted),
  and the full envelope capped at 64 KB. The adapter already returns this
  envelope — no `app/tools/adapter.py` changes needed.

A fast-path hit therefore produces: `chat` (synthetic tool call) →
`execute_tool trigger_automation` → `chat` (templated reply), all under one
request root, with `requests.path = fast_path`.

### Snapshots

Hashes are SHA-256 over canonical JSON, truncated to 16 hex chars; each snapshot
is written once per hash to its own table.

- **Prompt hash** — over the **base** prompt from `build_system_prompt`, not the
  per-call prompt (`ContextWindowMiddleware` injects the current time every call,
  which would make every hash unique).
- **Toolset hash** — over `[{name, description, args_schema}]` for the full
  registry at build time, sorted by name. The per-call subset is recorded
  separately as the offered-tools list.
- **Menu hash** — over menu items (`entity_id`, `name`, `description`) exactly as
  sent to the backend. **Not** `Menu.signature`, which hashes only the sorted
  entity-id set (so a description edit leaves it unchanged); `signature` stays
  the remote server's compile-cache key.

Prompt and toolset snapshots are registered at agent build time; menu snapshots
on menu refresh.

### Dataset

`/data/telemetry.sqlite`, WAL mode, one connection owned by the exporter thread,
numbered SQL migrations applied at startup. Tables (full DDL in the detailed
design doc):

- `requests` — one row per request (PK `request_id`), with `path`, `outcome`,
  input/output text, hashes, step count, timings.
- `model_calls` — one row per `chat` span, incl. offered tools, thinking,
  content, tool calls, token usage, `fast_path` flag.
- `tool_calls` — one row per `execute_tool` span, incl. args, status, error code,
  result envelope.
- `fast_path_decisions` — one row per classified request, incl. prediction,
  confidence, threshold, accepted, `skip_reason`.
- `labels` — feedback from any source (`ui` | `manual` | `tool:<name>`), keyed by
  `request_id`; the only table external labelling tools write to.
- `prompt_snapshots`, `toolset_snapshots`, `menu_snapshots` — hash → content.

Derived views: `fast_path_dataset` (one row per classified request, with the
agent's first `trigger_automation` entity for misses as a **reference column**,
not a label), `tool_offer_stats` (offered/chosen/error/`repeated_call` rates per
toolset hash, fast-path steps excluded), and `request_quality` (steps, thinking
volume, repeated calls, distinct tools). Confusion heuristics (backtracking,
overthinking, retry loops) are candidate views to validate against labels before
trusting.

### Labels API, UI, and export

- `POST /api/labels` — `{request_id, source, rating?, correct_tool?,
  correct_entity_id?, note?}`. External tools use the same endpoint or write to
  `labels` directly.
- Chat UI: thumbs up/down under each reply (`source=ui`); on thumbs-down, an
  optional "should have been" picker (tools, menu automations, "none").
  `request_id` is returned to clients (new `ChatResponse` field and SSE
  `done`/`error` events; the Assist component reads only `reply`, so it is safe).
- Metrics tab: `GET /api/telemetry/summary?days=` and
  `GET /api/telemetry/requests?limit=&cursor=` feed a new vanilla-JS tab with
  no-CDN charts. First panels: requests/day by path, duration & TTFT
  percentiles, tool offered/chosen/error table, outcome rates, fast-path hit
  rate + confidence histogram, and label counts.
- Export CLI: `python -m app.telemetry.export fast-path` (one JSONL line per
  classified request, menu resolved from `menu_snapshots`) and `... evals`
  (entries in the existing `tests/evals/cases.yaml` format from labelled
  requests, for manual curation). Production data proposes cases; description
  changes are judged offline on the curated set, not by comparing production
  weeks.

## Configuration

New `Settings` fields in `app/config.py`, mirrored in `config.yaml`'s options
schema (field names must match exactly, per the addon convention):

| Option | Default | Notes |
|---|---|---|
| `telemetry_enabled` | `true` | `false` = no provider, no-op tracer, middleware not added |
| `telemetry_db_path` | `/data/telemetry.sqlite` | empty = SQLite exporter disabled |
| `telemetry_retention_days` | `0` | `0` = keep forever; otherwise nightly prune |
| `otlp_endpoint` | `""` | Phase 2; empty = disabled; must resolve to a LAN address |

Existing `needle_*` options keep their meaning and now configure
`FastPathMiddleware` with the Needle backend. New dependencies:
`opentelemetry-api`, `opentelemetry-sdk` (Phase 2 adds
`opentelemetry-exporter-otlp-proto-http`).

**Do not** set `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` or SDK span limits — they
apply at span creation and would truncate the dataset. Any truncation for
Grafana happens later, in the Collector.

## Privacy and locality

- No exporters other than SQLite and (Phase 2) an explicit LAN OTLP endpoint.
  Validate at startup that `otlp_endpoint` resolves to a private address; refuse
  otherwise.
- Ensure `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2` are unset in the image.
- The dataset contains presence-related tool results
  (`get_person_locations`) and household activity. Add-on `/data` is in HA
  backups; if backups go off-site, exclude the file via `backup_exclude` in
  `config.yaml` and back it up separately. Verify the exclude pattern against
  current add-on docs.

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

Every attribute extraction in `TelemetryMiddleware` is individually wrapped: a
telemetry exception is logged and swallowed, and the handler result is always
returned. The step counter lives in a contextvar set by the request span, not on
the middleware instance (the agent is shared across concurrent requests).

## Testing

All tests run under `uv run pytest -q` with fakes — no HA/Ollama/collector — and
add no new warnings.

**Fast path:** hit (`FakeBackend` above threshold) yields
`Human → AI(tool_call fastpath-*) → Tool → AI(templated)` with the LLM fake
never called; miss falls through to the LLM with no synthetic messages; no
mid-loop interception after step 1; a second turn sees the fast-path turn in
history; the AI-actions gate and tier-2 audit still fire; backend error and the
opt-out flag both fall through; the middleware is absent when
`trigger_automation` is not registered; streaming emits the fast-path reply as
`token` + `done` with the `isinstance(msg, AIMessage)` check and does not
duplicate a normal LLM reply.

**Telemetry:** `InMemorySpanExporter` fixtures map to the right rows per table;
with subsetting enabled `model_calls.tools_offered` equals the subset; a
fast-path hit produces two `fast_path`-flagged `chat` spans, one
`fast_path.classify`, one `execute_tool`, and `requests.path = fast_path`; all
spans share the request's trace_id under concurrent requests; `split_thinking`
handles reasoning-field and inline-`<think>` inputs; two calls in one request
(different injected time) produce the same `prompt_hash`; a description change
yields a new `menu_hash` but the same `Menu.signature`; a read-only DB path and a
raising exporter still let `/api/chat` reply; a mid-stream disconnect yields
`outcome=cancelled`; and `export evals` output parses with
`tests/evals/run.py`'s loader.

## Risks & open questions

- **TTFT on non-streaming calls.** The middleware sees complete messages only;
  TTFT is recorded for the streaming endpoint only.
- **Token-usage availability.** Depends on the provider returning usage; fields
  stay NULL otherwise.
- **Synthetic messages in history.** The LLM will see fast-path tool calls it did
  not produce in later turns. Expected harmless (standard message format); watch
  for confusion in follow-ups.
- **Fast-path reply on trigger failure.** Templated for speed; handing that step
  to the LLM for a better explanation is a possible later option.
- **Semantic-convention churn.** GenAI conventions are experimental; names are
  pinned in `conventions.py`.
- **DB growth.** Estimated a few MB/day with full thinking; the 64 KB tool-result
  cap is the lever if large results (e.g. `get_history`) dominate.
- **llama.cpp streaming duplication.** Verified no duplicate full `AIMessage`
  with a fake streaming model; confirm once against the real provider after the
  `isinstance` change.

## Rollout

1. **Phase 0a — Fast path as middleware.** `app/fast_path/`, the backend
   protocol, the middleware, removal of the fast-path branches in `main.py`, the
   streaming `isinstance` fix. Behaviour-preserving apart from persisted history.
2. **Phase 0b — Dataset.** `app/telemetry/` package, SQLite exporter, schema,
   `TelemetryMiddleware`, request spans, snapshots, `request_id` in responses.
   Deploy and start collecting.
3. **Phase 1 — Labels & UI.** Labels endpoint + thumbs, metrics tab, export CLI,
   views.
4. **Phase 2 — Operational stack (optional, reserved).** OTLP exporter; a
   docker-compose Collector + Tempo + Prometheus + Grafana stack. No
   re-instrumentation required.
5. **Phase 3 — Event decisions (reserved).** Built with memory/event
   subscriptions; root span `handle_event gosling`, filtered-out events recorded
   with a reason, "decided not to act" as an explicit outcome, memory refs and a
   linked `verify_effect` span. A reserved `event_decisions` table shape is
   sketched in the detailed design doc.

v1 ships Phases 0a–1. The fast path stays behind `needle_enabled`, and telemetry
behind `telemetry_enabled` (default on, fail-open).
