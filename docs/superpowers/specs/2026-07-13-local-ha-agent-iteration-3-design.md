# Local HA Agent — Iteration 3 Design Spec (Assist, Streaming, UI)

**Date:** 2026-07-13 (revised 2026-09-02)
**Status:** Approved design; **Task 1 (checkpointer) merged**, remainder pre-implementation
**Builds on:** iteration 1 (merged) and iteration 2 (**merged** — control +
audit: `ToolContext.audit`, ACTION-tier gate, allowlist `call_service`,
`trigger_automation`; spec `2026-07-13-local-ha-agent-iteration-2-design.md`)

> **Revision note (2026-09-02):** terminology updated for HA release 2026.2,
> which renamed **"Add-ons" → "Apps"** to distinguish them from integrations.
> This deliverable is a Home Assistant **App** (formerly add-on). Two things
> landed after the original draft: a **Needle fast-path router** in `/api/chat`
> (triggers `automation.ai_*` without the LLM) and a POC **OpenAI-compatible
> `/v1` shim** used to test the voice pipeline against HA's built-in OpenAI
> Conversation integration. The shim is being **removed** — the custom
> `ConversationEntity` below + `/api/chat` is the Assist path of record.

> **Revision note (2026-09-11):** audit pass against main. Component 4
> (persistent memory) is **implemented and merged** (commit 00a582a) —
> corrected below to as-built. The other components (Assist component, SSE
> streaming, chat UI) remain pre-implementation.

## Overview

Iteration 3 makes the agent usable from Home Assistant's own surfaces: it
becomes a **conversation agent for Assist** (voice/chat via HA's pipeline)
through a small custom component, the HTTP API gains **streaming**, and the
ingress chat UI gets its first real investment (streamed replies, tool-call
indicators). The App core barely changes — this iteration is mostly a new
integration boundary and presentation work.

## Goals

- Talk to the agent through HA Assist (text first; voice works automatically
  wherever the user has STT/TTS pipelines configured).
- Streamed responses over HTTP (SSE) for the chat UI; REPL already streams.
- Chat UI: streaming display, minimal markdown rendering, visible tool
  activity, per-session thread.
- Optional persistent conversation memory across App restarts.

## Non-goals

- STT/TTS/wake word — HA's Assist pipeline owns those.
- Multi-user identity or per-user memory (ingress + Assist are house-scoped).
- Publishing the companion `ConversationEntity` component to **HACS default
  repos**. The product installed by the user is the **App**; the component is a
  thin bridge shipped alongside it and installed by manual copy into
  `config/custom_components/` or as a HACS *custom repository*. Default-repo
  submission is polish for later, not this iteration.
- New tools or tier changes (iteration 2 owns capability scope).
- Frontend frameworks/build steps — the UI stays vanilla JS served by
  FastAPI static files with ingress-relative paths.

## Component 1: Assist custom component

A separate top-level directory in this repo, shipped independently of the
App container:

```
custom_components/local_ha_agent/
├── __init__.py          # setup, forward to conversation platform
├── manifest.json        # domain: local_ha_agent, iot_class: local_polling
├── config_flow.py       # UI config: App base URL (+ optional API token later)
├── conversation.py      # ConversationEntity implementation
└── strings.json / translations/
```

- `conversation.py` implements HA's conversation entity
  (`conversation.ConversationEntity`, `async_process`): forwards the user
  text to the App's `POST /api/chat`, returns the reply as the
  conversation response.
- **Thread mapping:** HA supplies a `conversation_id`; it maps 1:1 to the
  App's `thread_id`, so Assist follow-ups share agent memory exactly like
  chat-UI turns.
- **App discovery:** config flow takes a base URL, defaulting to the App's
  internal hostname (`http://<slug>:8099`, resolvable from HA core when the App
  runs on the same host). Because it is just a URL, the same component also
  works when the agent runs **standalone** (e.g. a Docker container on the
  Ollama box) — point the base URL at that host. No Supervisor discovery
  protocol in this iteration — manual URL keeps the component decoupled from
  Supervisor APIs.
- **Needle fast-path:** the component POSTs to `/api/chat`, which already runs
  the Needle fast-path router before the agent, so `automation.ai_*` triggers
  resolve without the LLM. The component gets this for free — no component-side
  work.
- **Latency contract:** Assist pipelines time out long before a 60-second
  local generation. The component sets its HTTP timeout to 90 s, but the
  spec accepts that slow models degrade the Assist experience; the README
  documents "Assist wants a fast model" (e.g. the 4B on GPU) and that the
  chat UI/REPL remain the home for long-running questions. No partial-
  response hack for Assist in this iteration.
- **Errors:** App unreachable / agent error → a spoken-friendly error
  sentence via the conversation response's error field, never a stack trace.
- Distribution: README instructions for copying to `config/custom_components/`
  or adding the repo as a HACS custom repository. Versioned in
  `manifest.json` independently of the App version.

## Component 2: HTTP streaming (SSE)

- New endpoint `POST /api/chat/stream` (SSE over a POST body; EventSource
  can't POST, so the frontend uses `fetch` + `ReadableStream` parsing —
  keeps the API shape symmetric with `/api/chat`).
- Event protocol (one JSON object per SSE `data:` line):
  - `{"type": "token", "text": "..."}` — assistant text delta
    (AIMessageChunk string content, as the CLI filters today),
  - `{"type": "tool_call", "name": "...", "args": {...}}` — emitted when a
    tool call starts (from the same stream metadata; this is the UI's
    "agent is looking things up" indicator),
  - `{"type": "tool_result", "name": "...", "status": "ok|error"}`,
  - `{"type": "done", "reply": "<full text>"}` — terminal event, also
    carries the assembled reply so clients can reconcile,
  - `{"type": "error", "message": "..."}` — terminal error event
    (including the graceful recursion-limit message).
- Implementation: `agent.astream(..., stream_mode="messages")` wrapped in a
  small translator shared with nothing (the CLI keeps its simpler inline
  filter — two consumers, two very different presentations; revisit DRY
  only if a third consumer appears).
- **Fast-path interaction:** the translator streams the *agent* only, but
  `/api/chat` runs the Needle fast-path first. The streaming endpoint must
  mirror that: run the fast-path before streaming, and on a hit emit a single
  `token` + terminal `done` (there is nothing to stream — no LLM ran). Only on
  a miss does it hand off to `agent.astream`.
- `/api/chat` (non-streaming) remains, unchanged — it is the Assist
  component's contract and the simpler integration surface. The POC
  OpenAI-compat `/v1/chat/completions` + `/v1/models` shim is **removed** this
  iteration (superseded by the `ConversationEntity` → `/api/chat` path).
- Uvicorn/ingress keep-alive settings from iteration 1 already cover
  long-lived SSE responses; verify with a >60 s generation as part of DoD.

## Component 3: Chat UI investment

Still a single static page, no build step:

- Streaming rendering via the SSE protocol above (token append).
- Tool activity line ("🔍 get_history …") shown while tool events arrive,
  collapsed after the reply completes.
- Minimal markdown: code blocks, bold, lists (a small vendored renderer or
  hand-rolled subset — no CDN dependency; the App must work offline).
- Thread handling: a per-browser-session `thread_id` (crypto.randomUUID
  persisted in sessionStorage) plus a "New conversation" button.
- Errors surface inline in the chat (from `error` events / non-200s).
- Relative paths throughout (ingress prefix), as today.

## Component 4: Optional persistent memory (LANDED — commit 00a582a)

- Selection toggles on the **`checkpoint_db_path`** `Settings` field, not a
  `checkpointer` enum. Empty (the default) ⇒ the existing
  **`BoundedMemorySaver`** (LRU thread eviction, in `app/agent/memory.py`) —
  the `build_agent` default — not a plain `MemorySaver`. A non-empty path is
  the only behavioral change.
- A non-empty `checkpoint_db_path` selects langgraph's **`AsyncSqliteSaver`**
  (wired in `app/agent/checkpointer.py` via `open_checkpointer`), writing to
  that path (survives App restarts; `/data` is the App's persistent volume).
  The sqlite default path in `config.yaml` is **`/data/checkpoints.sqlite`**.
  Dev CLI uses a local path from settings.
- Trimming middleware already bounds what reaches the model, so an
  ever-growing store is a disk concern, not a context concern; the spec
  accepts unbounded growth for now and documents where the file lives.

## Security notes

- The App's API becomes reachable by the custom component over the
  internal network (not just via ingress). The component→App hop is
  unauthenticated in this iteration, same trust domain as the LAN;
  documented explicitly. If the App port is ever published beyond the
  host, an API token option must be added first (config_flow already
  reserves the field).
- SSE endpoint enforces the same origin policy as the rest of the app
  (ingress handles authentication for browser access).

## Testing

- **Custom component:** pytest with a fake App HTTP server —
  async_process happy path, conversation_id→thread_id mapping, timeout and
  unreachable-App error shaping. Default choice:
  `pytest-homeassistant-custom-component`; if that harness fights the
  Python 3.14 venv, fall back to thin fakes of the conversation entity's
  dependencies and note the downgrade in the plan.
- **SSE endpoint:** TestClient-based tests with a fake agent emitting a
  scripted stream: assert event ordering (tool_call → tokens → done),
  error-event terminalization, and recursion-limit → error event.
- **UI:** manual checklist (stream renders, tool indicator, new-conversation
  resets thread, works under ingress prefix).
- **Assist e2e:** manual checklist against a real HA instance (text chat
  first, then a voice pipeline if configured).

## Definition of done

- Typing a question in HA's Assist chat (with the component configured)
  returns an agent answer that used a tool; follow-up question in the same
  Assist conversation shares context.
- Chat UI shows tokens as they generate and a tool-activity indicator;
  a >60 s generation completes without proxy/keep-alive termination.
- With `checkpoint_db_path` set (SQLite checkpointer), conversation context
  survives an App restart intact.
- Iterations 1–2 behavior unchanged when the component isn't installed and
  the UI is the old one (endpoints are additive).
