---
title: Needle fast-path router for low-latency automation triggering
date: 2026-08-25
status: draft (design approved, not implemented)
supersedes: none
related: 2026-08-24-automation-mediated-control-design.md
---

# Needle fast-path router

## Summary

Add an optional **fast path** in front of the LangChain agent: a tiny local
tool-calling model ([Needle 2](https://huggingface.co/Cactus-Compute/needle2),
45M params, Apache-2.0) classifies each incoming utterance and, when it is
confident the user wants to trigger a specific `automation.ai_*` automation,
fires it directly — bypassing the big model entirely. Anything it is not
confident about falls through to the existing agent unchanged.

The fast path invokes the **existing `trigger_automation` `StructuredTool`**, so
the `ai_actions_switch` gate, the audit record, and the loop-guard all fire
exactly as they do for the agent. It is additive: remove it (or set
`needle_enabled: false`) and behavior is byte-identical to today.

## Motivation

Two independent drivers:

1. **Latency under hardware constraints.** The addon runs in a VM on a
   Proxmox host (mid-2011 Mac mini, 16 GB RAM); the big model runs on a
   separate Ollama box at `192.168.1.4`. A simple "run the goodnight
   automation" request today pays: network hop to the Ollama box + the big
   model's multi-step agent loop (system prompt + tool schemas + reasoning
   tokens). Measured end-to-end this exceeds ~10 s, especially on a cold
   model. The target for the common "trigger X" case is ~1–2 s. Needle on the
   HA host makes that case **fully local and single-shot** — no network hop,
   no big-model reasoning.

2. **Grammar-constrained tool calls, which the current stack structurally
   cannot do.** Verified empirically: Ollama's `format` (structured decoding)
   and native tool-calling are mutually exclusive — setting `format` nulls
   `tool_calls`. So today we cannot constrain a tool call's argument values,
   and the residual tool-selection eval failures are a small-model
   instruction-following ceiling. Needle compiles a **byte-level grammar from
   the tool schemas**, so its output is constrained to exactly the valid menu
   by construction.

## Goals

- Sub-2 s response for confident "trigger an `ai_*` automation" utterances.
- Preserve the read-only / menu-only / gated-write guarantees with zero
  weakening — the fast path is a new *caller* of an existing gated tool, never
  a new write path.
- Fail open: any fast-path problem degrades to the existing agent, never an
  error and never a blocked request.
- Be measurable before it is trusted (eval harness, dedicated case file).

## Non-goals (v1)

- Needle handling read/query requests — reads fall through to the agent, which
  has the reasoning and context a 256-token model lacks.
- Direct device control — every action goes through an automation (project
  rule); Needle only ever picks an `automation.ai_*` id.
- Frontend JSON transformation / skipping tool-listing — iteration 3 territory.
- Fine-tuning Needle — start zero-shot with grammar constraints; fine-tune only
  if the eval demands it.
- Confirmation prompts / near-miss recovery UI — one threshold, two outcomes.
- Streaming.

## Design decisions (locked during brainstorming)

- **Confident case → trigger immediately.** No confirmation step. The
  `ai_actions_switch` gate + the confidence threshold are the safety; that is
  where the latency win comes from.
- **Unsure case (below threshold, or no trigger produced) → fall through to the
  full agent.** One fallback path for everything, which also transparently
  covers reads (Needle won't confidently emit a trigger for "what's the
  temperature").
- **Runtime: in-process, model loaded once (approach A), with a persistent
  local sidecar (approach B) as a pre-designed fallback** if the in-process
  binding proves unavailable on the target arch. Per-request subprocess was
  rejected — it re-pays load + grammar compilation every call.

## Architecture

### Module layout — new package `app/needle/`

Kept out of `app/agent/` on purpose: this concern sits *above* the agent (it
decides whether the agent runs at all).

- **`app/needle/menu.py`** — builds the fast-path menu: the list of
  `automation.ai_*` automations (friendly name → entity id) from REST, and
  compiles/caches the byte-level grammar from that id set. Single purpose:
  "what can be triggered, and the grammar that constrains Needle to exactly
  those ids."
- **`app/needle/backend.py`** — `NeedleBackend` protocol + `CactusBackend`
  (in-process, approach A) + `FakeBackend` (tests). The swappable seam:
  approach A→B never touches the router.
- **`app/needle/router.py`** — `FastPathRouter`: orchestrates
  menu → backend → threshold decision → invoke `trigger_automation`, or return
  `None`. Owns no inference and no HA calls of its own.

Each unit is independently testable; only `CactusBackend` needs the real
`cactus` runtime.

### Interfaces

```python
@dataclass
class Decision:
    entity_id: str | None   # grammar-constrained to the current ai_* menu
    confidence: float

class NeedleBackend(Protocol):
    async def classify(self, message: str, menu: Menu) -> Decision: ...

class FastPathRouter:
    def __init__(self, backend, trigger_tool, menu_provider, threshold): ...

    async def try_fast_path(self, message: str, thread_id: str) -> str | None:
        """Return a reply string if the fast path handled the turn, else None
        (→ caller runs the agent). Never raises."""
```

**Load-bearing invariant.** On a confident hit the router calls
`trigger_tool.ainvoke({"entity_id": id}, config={"configurable": {"thread_id": ...}})`
— the *existing* `StructuredTool` from `app/tools/adapter.py`. It never touches
`ctx.rest` or the handler directly. The `ai_actions_switch` gate, the audit
record, and the loop-guard therefore fire exactly as for the agent. The router
cannot become a second, ungated write path — which keeps the CLAUDE.md
"menu-only + gated in adapter.py" hard rule structurally true.

### Menu & grammar

Needle's entire vocabulary on the write path is the set of `automation.ai_*`
ids (`AI_AUTOMATION_PREFIX`, imported from
`app/tools/action/trigger_automation.py` — single source of truth). `menu.py`
fetches them with their friendly names (so Needle can map "goodnight" →
`automation.ai_goodnight`) and compiles the grammar so Needle **cannot emit an
id outside the current menu**. Combined with `trigger_automation`'s own prefix
check in the handler, that is a double structural guard: proposing a non-menu
automation is impossible by construction, and even a bug cannot get past the
handler.

**Freshness.** Cache the menu + compiled grammar keyed by a hash of the sorted
id set, TTL `needle_menu_ttl_s` (default 60 s). Rebuild the grammar only when
the id set changes — recompiling per request would add avoidable latency. If
the menu fetch fails, the router returns `None` (fall through) — never blocks.

### Data flow

```
POST /api/chat {message, thread_id}
  |
  |-- reply = await router.try_fast_path(message, thread_id)
  |      1. menu = menu_provider.get()            # cached ai_* ids + grammar
  |      2. decision = backend.classify(...)       # Needle inference (warm, local)
  |      3. if decision.entity_id and decision.confidence >= threshold:
  |            env = await trigger_tool.ainvoke({entity_id}, config)  # gate + audit fire
  |            return human_reply(env)   # ok -> "Ran X"; ai_disabled -> refusal text
  |         else:
  |            return None
  |
  |-- reply is not None  -> ChatResponse(reply)              # fast path, ~1 s
  '-- reply is None      -> app.state.agent.ainvoke(...)     # today's path, unchanged
```

**Gate refusal does not fall through.** If the gate refuses (`ai_disabled` /
`ai_gate_unavailable`), the router returns that refusal as the reply — the
agent would hit the same switch and merely be slower. Fall-through is only for
below-threshold / non-action utterances.

## Configuration

New `Settings` fields in `app/config.py`, mirrored in `config.yaml`'s options
schema (field names must match exactly, per the addon convention):

| Field | Default | Purpose |
|-------|---------|---------|
| `needle_enabled` | `false` | Opt-in **and** kill switch. False ⇒ `try_fast_path` returns `None` unconditionally; behavior byte-identical to today. |
| `needle_model_path` | — | Path to the `.cact` binary. |
| `needle_confidence_threshold` | `0.85` | The KPI knob, tuned against the eval. Decision uses `>=`. |
| `needle_menu_ttl_s` | `60` | Menu/grammar cache TTL. |
| `needle_backend` | `"cactus"` | `"cactus"` (A, in-process) or `"sidecar"` (B). |
| `needle_sidecar_url` | — | Used only by backend B. |

The router is built in the `main.py` lifespan next to `agent` and injected — no
import-time singletons.

## Error handling & degradation (fail open)

The mirror of "handlers never raise into the agent loop," one level up: **the
fast path never breaks a request.** Any exception in menu fetch, grammar
compile, or Needle inference is caught, logged under a dedicated `needle`
logger (so fast-path health is visible separately from the agent), and turned
into `None` → the agent handles the turn.

- Gate refusal — *not* an error; a valid fast reply, returned as-is.
- Empty menu (no `ai_*` automations) → `None`.
- Backend raises / times out → `None`.

## Testing

- **Unit (fast, no HA / Ollama / cactus)** — drive `FastPathRouter` with
  `FakeBackend`:
  - above-threshold → fires and returns the reply;
  - below-threshold → `None`;
  - backend raises → `None`;
  - empty menu → `None`;
  - gate refusal → surfaced as reply, *not* fallen through.
  Assert the confident hit goes *through* the real `trigger_automation`
  `StructuredTool` so the gate + audit fire (reuse the existing gate-test
  style). No new pytest warnings.
- **Eval (go/no-go measurement)** — add Needle as a selectable backend in
  `tests/evals/run.py`, reading a **dedicated `tests/evals/cases-needle.yaml`**
  (separate from `cases.yaml`) that holds trigger-automation utterances. This
  yields the number that decides everything: **confident-fraction × accuracy on
  the trigger subset**, measured against the ~22–23/31 `minicpm-ha` baseline.
  Not collected by pytest (needs the live runtime).
- **Feasibility spike (step 0, throwaway)** — a standalone script that loads a
  `.cact`, runs one grammar-constrained `classify`, and reads back a
  confidence, on the addon's target arch. Gates approach A vs the B fallback
  *before* any integration work. Labeled throwaway, not kept.

## Risks & open questions

- **`cactus` Python binding on the target arch.** The mid-2011 Mac mini is
  Sandy Bridge (AVX1, no AVX2); `cactus` is mobile/ARM-first. The step-0 spike
  resolves whether approach A is viable; approach B is the pre-designed
  fallback and requires no router redesign.
- **Confident-fraction is the real KPI.** If Needle confidently and correctly
  handles a large share of "trigger X" utterances, the latency problem is
  solved for the bulk of daily use. If that fraction is low, the value case
  weakens — hence measuring before trusting.
- **Threshold calibration.** `0.85` is a starting point; tune against
  `cases-needle.yaml`. Too low → wrong automations fire; too high → everything
  falls through and the win evaporates.

## Rollout

Ships disabled (`needle_enabled: false`). Enable in a dev config, populate a
few test `automation.ai_*` automations, verify correctness and latency
manually and via the eval, then tune the threshold. Only then consider it for
normal use.
