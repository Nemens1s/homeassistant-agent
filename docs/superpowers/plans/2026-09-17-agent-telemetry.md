# Agent Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Needle fast path into a `FastPathMiddleware` and add an OTel-instrumented, full-fidelity local SQLite interaction dataset with a labels store, metrics tab, and export CLI.

**Architecture:** A backend-agnostic `FastPathMiddleware` short-circuits `awrap_model_call` so a fast-path hit becomes a normal (persisted, traced, gated) agent step. A `TelemetryMiddleware` sitting just outside it emits OTel spans for every model and tool step; a custom `SqliteSpanExporter` maps those spans into a dataset in `/data/telemetry.sqlite`. Phase 1 adds a labels endpoint, a metrics tab, and export commands over that dataset.

**Tech Stack:** Python 3.14, `uv`, LangChain 1.3.16 / LangGraph 1.2.11, `opentelemetry-api` + `opentelemetry-sdk`, SQLite (stdlib `sqlite3`), FastAPI, pytest, vanilla-JS frontend.

**Spec:** `docs/superpowers/specs/2026-09-17-agent-telemetry-design.md` (detailed design: `docs/superpowers/plans/2026-09-17-agent-telemetry-design.md`)

## Global Constraints

Every task's requirements implicitly include this section. Values copied verbatim from the spec / CLAUDE.md:

- **Python is always `uv run python` / `uv pip`.** Tests: `uv run pytest -q`.
- **No new pytest warnings.** Exactly one known warning (starlette TestClient deprecation) is expected; any new warning is a finding.
- **langchain v1 API only** (`create_agent`, `langchain.agents.middleware`). Verify API details against the installed venv with `inspect`; do not trust training data.
- **Pinned versions:** `langchain==1.3.16`, `langgraph==1.2.11`.
- **No import-time singletons** — everything is built by factories with injected settings.
- **Prefer plain, readable Python:** regular `for` loops over comprehensions, explicit steps over clever one-liners.
- **Tool output is always `ToolResult(...).to_json()` compact JSON** — never `str(dict)`, never raw exceptions. Handlers never raise into the agent loop.
- **Writes stay menu-only and gated.** The fast path is a new *caller* of the existing gated `trigger_automation` tool, never a new write path. Do not touch the gate in `app/tools/adapter.py`.
- **Telemetry never blocks or fails a request (fail open).** Every telemetry attribute extraction is individually wrapped; exceptions are logged and swallowed and the handler result is returned unchanged.
- **No truncation in the dataset.** Do **not** set `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` or SDK span limits. The only cap is a 64 KB cap on the tool-result envelope, applied by the exporter.
- **Nothing leaves the LAN.** Only the SQLite exporter in v1. `LANGSMITH_TRACING` / `LANGCHAIN_TRACING_V2` must be unset.
- **New `Settings` fields must be mirrored in `config.yaml`'s options schema with identical names** (addon reads `/data/options.json` by field name).
- **`needle_confidence_threshold` default `0.0` is intentional** ("trust the grammar-constrained call"): fine-tuned weights report confidence `None` → coerced to `0.0`. Do not change it.
- Use a feature branch + PR (no direct commits to `main`). Commit after every task.

## Test Conventions (house style — verified against the suite)

**The suite is flat** — there are no test subpackages. Every new test file named in this plan already uses a flat, area-prefixed name directly under `tests/` (`tests/test_fast_path_*.py`, `tests/test_telemetry_*.py`). Keep them there; do not create `tests/fast_path/` or `tests/telemetry/` directories.

The area prefix is deliberate, not cosmetic: it avoids two collisions a bare strip-the-subdir would cause — Task 14's telemetry config test vs the **existing** `tests/test_config.py`, and the fast-path vs telemetry `test_middleware.py` (now `test_fast_path_middleware.py` and `test_telemetry_middleware.py`).

Other pinned idioms (copy them; don't reinvent):

- **Async tests need no decorator** — `pyproject.toml` sets `asyncio_mode = "auto"`, so a bare `async def test_...` runs. The `@pytest.mark.asyncio` in the snippets is redundant but harmless; keep or drop it.
- **Build `Settings` with `Settings(_env_file=None, ...)`** so a developer's `.env` never leaks in (`tests/conftest.py` also clears the env vars).
- **Agent-graph tests** use `langchain_core.language_models.fake_chat_models.FakeMessagesListChatModel` (subclassed to make `bind_tools` return `self`), call `registry._reset_for_tests()` before and after, build via `factory.build_agent(settings, ctx)`, and monkeypatch `app.agent.factory.build_llm` to return the scripted model. See `tests/test_e2e_graph.py` for the canonical example.
- **`Decision` / `FakeBackend` import from `app.fast_path.backend`** after Task 1 (the old `app.needle.backend` import in `tests/test_needle_router.py` disappears when that test is deleted with the router).
- **`ToolContext(settings=..., rest=FakeRest(), ws=None)`** — a minimal `FakeRest` with `get_state`/`list_states` is the norm (see `tests/test_e2e_graph.py`).

---

## File Structure

**Phase 0a — fast path as middleware**
- Create `app/fast_path/__init__.py` — package marker.
- Create `app/fast_path/backend.py` — `FastPathBackend` protocol, `Decision`, `FakeBackend` (moved from `app/needle/backend.py`).
- Create `app/fast_path/middleware.py` — `FastPathMiddleware`, `_reply_from_envelope` (moved from `app/needle/router.py`).
- Modify `app/needle/backend.py` — drop `Decision`/`FakeBackend`/`NeedleBackend` protocol; re-export `Decision` from `app.fast_path.backend` for the helpers that build it.
- Modify `app/needle/remote_backend.py` — add `name = "needle-remote"`; import `Decision` from `app.fast_path.backend`; drop the `print`.
- Modify `app/needle/factory.py` — `build_fast_path_backend(cfg, rest, ctx)` returns `(backend, menu_provider)` or `None`; no `FastPathRouter`.
- Delete `app/needle/router.py`.
- Modify `app/agent/middleware/__init__.py` — `build_middleware` gains optional fast-path params and appends `FastPathMiddleware`.
- Modify `app/agent/factory.py` — `build_agent` accepts `fast_path=None`.
- Modify `app/main.py` — remove fast-path branches + `app.state.fast_path`; build backend+menu in lifespan; pass to `build_agent`.
- Modify `app/agent/streaming.py` — `isinstance(msg, AIMessage)`.

**Phase 0b — dataset & instrumentation**
- Create `app/telemetry/__init__.py`, `thinking.py`, `conventions.py`, `store.py`, `snapshots.py`, `sqlite_exporter.py`, `setup.py`, `middleware.py`.
- Modify `app/agent/factory.py` / `app/agent/middleware/__init__.py` — register prompt/toolset snapshots at build time; add `TelemetryMiddleware`.
- Modify `app/main.py` + `app/cli.py` — request root span, `request_id` in responses, channel derivation, provider init/shutdown in lifespan.
- Modify `app/config.py` + `config.yaml` — telemetry options.
- Modify `pyproject.toml` — OTel deps.

**Phase 1 — labels, UI, export**
- Modify `app/telemetry/store.py` — read queries + derived views.
- Modify `app/main.py` — `POST /api/labels`, `GET /api/telemetry/summary`, `GET /api/telemetry/requests`.
- Create `frontend/metrics.*` — metrics tab.
- Create `app/telemetry/export.py` — `fast-path` and `evals` subcommands.

---

# Phase 0a — Fast path as middleware

## Task 1: `app/fast_path/backend.py` — backend-agnostic protocol

**Files:**
- Create: `app/fast_path/__init__.py` (empty)
- Create: `app/fast_path/backend.py`
- Modify: `app/needle/backend.py`, `app/needle/remote_backend.py`
- Test: `tests/test_fast_path_backend.py` (delete superseded `tests/test_needle_backend.py`)

**Interfaces:**
- Produces: `Decision(entity_id: str | None, confidence: float)` (frozen dataclass); `FastPathBackend` Protocol with `name: str` and `async def classify(self, message: str, menu: Menu) -> Decision`; `FakeBackend(decision=None, by_message=None)` with `name = "fake"`. The name-map helpers `_build_name_map`, `_decision_from_result`, `_tool_name` stay in `app/needle/backend.py` (Needle-specific).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fast_path_backend.py
import pytest
from app.fast_path.backend import Decision, FakeBackend
from app.needle.menu import Menu

_EMPTY_MENU = Menu(items=(), signature="")

@pytest.mark.asyncio
async def test_fakebackend_scripted_decision_and_name():
    b = FakeBackend(decision=Decision(entity_id="automation.ai_action_night", confidence=0.9))
    assert b.name == "fake"
    d = await b.classify("night", _EMPTY_MENU)
    assert d == Decision(entity_id="automation.ai_action_night", confidence=0.9)

@pytest.mark.asyncio
async def test_fakebackend_by_message_and_default_noop():
    b = FakeBackend(by_message={"night": Decision("automation.ai_action_night", 0.9)})
    assert (await b.classify("night", _EMPTY_MENU)).entity_id == "automation.ai_action_night"
    assert await b.classify("weather?", _EMPTY_MENU) == Decision(entity_id=None, confidence=0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fast_path_backend.py -v`
Expected: FAIL — `ModuleNotFoundError: app.fast_path.backend`.

- [ ] **Step 3: Write minimal implementation**

Create `app/fast_path/backend.py` by moving `Decision` and `FakeBackend` out of `app/needle/backend.py`, adding `name`:

```python
"""Backend-agnostic fast-path inference seam. Any classifier that maps an
utterance + menu to a Decision plugs in here; the middleware depends only on
this protocol, never on Needle."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.needle.menu import Menu


@dataclass(frozen=True)
class Decision:
    entity_id: str | None   # constrained to the current menu, or None
    confidence: float


class FastPathBackend(Protocol):
    name: str
    async def classify(self, message: str, menu: Menu) -> Decision: ...


class FakeBackend:
    """Test backend: per-message Decision if provided, else a scripted Decision,
    else a no-op (None, 0.0)."""

    name = "fake"

    def __init__(self, decision: Decision | None = None, by_message: dict | None = None):
        self._decision = decision
        self._by_message = by_message or {}

    async def classify(self, message: str, menu: Menu) -> Decision:
        if message in self._by_message:
            return self._by_message[message]
        if self._decision is not None:
            return self._decision
        return Decision(entity_id=None, confidence=0.0)
```

In `app/needle/backend.py`: delete `Decision`, `FakeBackend`, and the `NeedleBackend` Protocol; keep `_tool_name`, `_build_name_map`, `_decision_from_result`; add `from app.fast_path.backend import Decision` at the top so `_decision_from_result` still constructs it. Update the `_decision_from_result` return annotation from `"Decision"` to `Decision`.

In `app/needle/remote_backend.py`: change the import to `from app.fast_path.backend import Decision`; add class attribute `name = "needle-remote"`; delete the `print(f"Needle response {response}")` line.

Existing tests: `tests/test_needle_backend.py` tests `Decision`/`FakeBackend` and is superseded by the new `tests/test_fast_path_backend.py` — delete it. `tests/test_needle_backend_helpers.py` tests `_build_name_map`/`_decision_from_result`/`_tool_name`, which **stay** in `app/needle/backend.py`, so it keeps passing unchanged (it relies on `Decision` being importable there, which the new `from app.fast_path.backend import Decision` line provides).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fast_path_backend.py tests/ -q -k "needle or fast_path or backend"`
Expected: PASS; no new warnings.

- [ ] **Step 5: Commit**

```bash
git add app/fast_path app/needle/backend.py app/needle/remote_backend.py tests/test_fast_path_backend.py
git rm tests/test_needle_backend.py
git commit -m "refactor: extract backend-agnostic FastPathBackend from needle"
```

---

## Task 2: `FastPathMiddleware`

**Files:**
- Create: `app/fast_path/middleware.py`
- Test: `tests/test_fast_path_middleware.py`

**Interfaces:**
- Consumes: `FastPathBackend`, `Decision` (Task 1); `MenuProvider.get() -> Menu` from `app/needle/menu.py`; LangChain `AgentMiddleware`, `AIMessage`, `HumanMessage`, `ToolMessage`.
- Produces: `FastPathMiddleware(backend, menu_provider, threshold: float)`; module constant `FASTPATH_PREFIX = "fastpath-"`; `_reply_from_envelope(raw: str, entity_id: str) -> str` (moved from router). The synthetic first-step `AIMessage` has one `trigger_automation` tool call with `id` starting `FASTPATH_PREFIX` and `response_metadata = {"model_name": backend.name, "fast_path": True}`.

**Design notes for the implementer:**
- "First step of a turn" = `request.messages[-1]` is a `HumanMessage`.
- "Step after a fast-path tool call" = `request.messages[-1]` is a `ToolMessage` whose `tool_call_id` starts with `FASTPATH_PREFIX`.
- The middleware never needs `thread_id`: it only *emits* the tool call; the agent's tool node executes it with the graph's config, so the gate/audit/loop-guard fire as normal.
- Opt-out: read defensively from the runtime context; default enabled. No v1 caller disables it.
- Fail open: any exception in menu fetch or `classify` → log, call the handler.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fast_path_middleware.py
import json
import pytest
from types import SimpleNamespace
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.fast_path.backend import Decision, FakeBackend
from app.fast_path.middleware import FastPathMiddleware, FASTPATH_PREFIX
from app.needle.menu import Menu, MenuItem

_MENU = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="s")

class _FakeMenu:
    def __init__(self, menu): self._m = menu
    async def get(self): return self._m

def _request(messages, tools=None):
    return SimpleNamespace(messages=messages, tools=tools or [], runtime=None)

async def _fail_handler(request):
    raise AssertionError("handler must not be called")

@pytest.mark.asyncio
async def test_hit_returns_synthetic_tool_call():
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _FakeMenu(_MENU), threshold=0.0)
    resp = await mw.awrap_model_call(_request([HumanMessage("goodnight")]), _fail_handler)
    assert isinstance(resp, AIMessage)
    assert resp.response_metadata == {"model_name": "fake", "fast_path": True}
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc["name"] == "trigger_automation"
    assert tc["args"] == {"entity_id": "automation.ai_action_night"}
    assert tc["id"].startswith(FASTPATH_PREFIX)

@pytest.mark.asyncio
async def test_miss_calls_handler():
    mw = FastPathMiddleware(FakeBackend(Decision(None, 0.0)), _FakeMenu(_MENU), threshold=0.5)
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm reply")
    resp = await mw.awrap_model_call(_request([HumanMessage("what's the weather")]), handler)
    assert called == {"yes": True}
    assert resp.content == "llm reply"

@pytest.mark.asyncio
async def test_step_after_fastpath_tool_returns_templated_reply():
    mw = FastPathMiddleware(FakeBackend(), _FakeMenu(_MENU), threshold=0.0)
    ok = json.dumps({"status": "ok"})
    tm = ToolMessage(content=ok, tool_call_id=FASTPATH_PREFIX + "abc", name="trigger_automation")
    tm.additional_kwargs["fastpath_entity_id"] = "automation.ai_action_night"
    resp = await mw.awrap_model_call(_request([HumanMessage("x"), tm]), _fail_handler)
    assert isinstance(resp, AIMessage)
    assert "automation.ai_action_night" in resp.content

@pytest.mark.asyncio
async def test_mid_loop_tool_message_falls_through():
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _FakeMenu(_MENU), threshold=0.0)
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("continue")
    tm = ToolMessage(content="{}", tool_call_id="call_normal_123", name="get_weather")
    await mw.awrap_model_call(_request([HumanMessage("x"), AIMessage("t"), tm]), handler)
    assert called == {"yes": True}

@pytest.mark.asyncio
async def test_empty_menu_falls_through():
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _FakeMenu(Menu(items=(), signature="")), threshold=0.0)
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm")
    await mw.awrap_model_call(_request([HumanMessage("goodnight")]), handler)
    assert called == {"yes": True}

@pytest.mark.asyncio
async def test_backend_error_falls_through():
    class Boom:
        name = "boom"
        async def classify(self, message, menu): raise RuntimeError("down")
    mw = FastPathMiddleware(Boom(), _FakeMenu(_MENU), threshold=0.0)
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm")
    await mw.awrap_model_call(_request([HumanMessage("goodnight")]), handler)
    assert called == {"yes": True}

@pytest.mark.asyncio
async def test_opt_out_flag_falls_through():
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _FakeMenu(_MENU), threshold=0.0)
    req = SimpleNamespace(messages=[HumanMessage("goodnight")], tools=[],
                          runtime=SimpleNamespace(context={"fast_path": False}))
    called = {}
    async def handler(request):
        called["yes"] = True
        return AIMessage("llm")
    await mw.awrap_model_call(req, handler)
    assert called == {"yes": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fast_path_middleware.py -v`
Expected: FAIL — `ModuleNotFoundError: app.fast_path.middleware`.

- [ ] **Step 3: Write minimal implementation**

```python
# app/fast_path/middleware.py
"""Fast path as a model-call middleware. On a confident hit it short-circuits
the model call with a synthetic trigger_automation tool call; the agent's tool
node runs it (gate + audit + loop guard apply). The follow-up step returns the
templated reply. Never fails a request: any error falls through to the LLM."""

from __future__ import annotations

import json
import logging
import uuid

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

log = logging.getLogger("fast_path")

FASTPATH_PREFIX = "fastpath-"


def _reply_from_envelope(raw: str, entity_id: str) -> str:
    try:
        env = json.loads(raw)
    except (ValueError, TypeError):
        return "Sorry, I couldn't run that automation."
    if env.get("status") == "ok":
        return f"Done — triggered {entity_id}."
    error = env.get("error") or {}
    message = error.get("message")
    if message:
        return message
    return "Sorry, I couldn't run that automation."


def _opted_out(request) -> bool:
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None)
    if context is None:
        return False
    if isinstance(context, dict):
        return context.get("fast_path") is False
    return getattr(context, "fast_path", True) is False


class FastPathMiddleware(AgentMiddleware):
    def __init__(self, backend, menu_provider, threshold: float):
        super().__init__()
        self._backend = backend
        self._menu_provider = menu_provider
        self._threshold = threshold

    async def awrap_model_call(self, request, handler):
        messages = request.messages
        last = messages[-1] if messages else None

        # Step after a fast-path tool call → templated reply.
        if isinstance(last, ToolMessage) and str(last.tool_call_id).startswith(FASTPATH_PREFIX):
            entity_id = last.additional_kwargs.get("fastpath_entity_id", "")
            return AIMessage(content=_reply_from_envelope(last.content, entity_id))

        # First step of a turn → classify.
        if isinstance(last, HumanMessage) and not _opted_out(request):
            try:
                menu = await self._menu_provider.get()
                if not menu.items:
                    return await handler(request)
                decision = await self._backend.classify(last.content, menu)
            except Exception:
                log.exception("fast path error; falling through to agent")
                return await handler(request)
            if decision.entity_id is not None and decision.confidence >= self._threshold:
                return self._synthetic_call(decision.entity_id)

        return await handler(request)

    def _synthetic_call(self, entity_id: str) -> AIMessage:
        call_id = FASTPATH_PREFIX + uuid.uuid4().hex
        return AIMessage(
            content="",
            tool_calls=[{"name": "trigger_automation",
                         "args": {"entity_id": entity_id},
                         "id": call_id}],
            response_metadata={"model_name": self._backend.name, "fast_path": True},
            additional_kwargs={"fastpath_entity_id": entity_id},
        )
```

> Note for the implementer: the `fastpath_entity_id` is stashed in the synthetic AIMessage's `additional_kwargs` so the follow-up step can template the reply; the `ToolMessage` produced by the tool node does not carry it automatically. Task 4's integration test confirms it survives the tool node — if LangGraph strips `additional_kwargs` from the `ToolMessage`, fall back to parsing the entity id out of the tool args recorded on the preceding AIMessage (`messages[-2].tool_calls[0]["args"]["entity_id"]`). Prefer the `messages[-2]` lookup if the integration test in Task 4 shows the kwarg is not present on the ToolMessage.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fast_path_middleware.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/fast_path/middleware.py tests/test_fast_path_middleware.py
git commit -m "feat: add FastPathMiddleware short-circuiting model calls"
```

---

## Task 3: Wire the middleware into the agent build

**Files:**
- Modify: `app/agent/middleware/__init__.py`, `app/agent/factory.py`, `app/needle/factory.py`
- Test: `tests/test_fast_path_wiring.py`

**Interfaces:**
- Consumes: `FastPathMiddleware` (Task 2); existing `build_middleware(settings, guard, base_prompt, budget)`.
- Produces: `build_middleware(settings, guard, base_prompt, budget, fast_path=None)` where `fast_path` is `(backend, menu_provider)` or `None`; appends `FastPathMiddleware` when `fast_path is not None` and `settings.max_tier >= 2`. `build_agent(settings, ctx, checkpointer=None, fast_path=None)` forwards it. `app/needle/factory.build_fast_path_backend(cfg, rest, ctx) -> tuple | None` returns `(RemoteNeedleBackend, MenuProvider)` under the same conditions the old `build_fast_path_router` used.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fast_path_wiring.py
from app.agent.middleware import build_middleware
from app.fast_path.middleware import FastPathMiddleware
from app.fast_path.backend import FakeBackend
from app.tools.adapter import LoopGuard
from app.config import Settings

class _Menu:
    async def get(self): ...

def _types(mws):
    return [type(m).__name__ for m in mws]

def test_fastpath_added_when_provided_and_tier2():
    s = Settings(max_tier=2, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=(FakeBackend(), _Menu()))
    assert "FastPathMiddleware" in _types(mws)
    # outermost-first: FastPath is the last (innermost) entry
    assert isinstance(mws[-1], FastPathMiddleware)

def test_fastpath_absent_without_tier2():
    s = Settings(max_tier=1, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=(FakeBackend(), _Menu()))
    assert "FastPathMiddleware" not in _types(mws)

def test_fastpath_absent_when_none():
    s = Settings(max_tier=2, enable_tool_subsetting=False)
    mws = build_middleware(s, LoopGuard(), "p", 1024, fast_path=None)
    assert "FastPathMiddleware" not in _types(mws)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fast_path_wiring.py -v`
Expected: FAIL — `build_middleware() got an unexpected keyword argument 'fast_path'`.

- [ ] **Step 3: Write minimal implementation**

In `app/agent/middleware/__init__.py`, extend the signature and append the middleware last (innermost) so `TelemetryMiddleware`, added in Task 12, will sit outside it:

```python
from app.fast_path.middleware import FastPathMiddleware

def build_middleware(settings, guard, base_prompt, budget, fast_path=None):
    middleware = [
        ContextWindowMiddleware(base_prompt, budget),
        LoopGuardResetMiddleware(guard),
    ]
    if settings.max_history_messages > 0:
        middleware.append(HistoryCapMiddleware(settings.max_history_messages))
    if settings.enable_tool_subsetting:
        middleware.insert(0, ToolSubsetMiddleware())
    if fast_path is not None and settings.max_tier >= 2:
        backend, menu_provider = fast_path
        middleware.append(FastPathMiddleware(backend, menu_provider, settings.needle_confidence_threshold))
    return middleware
```

In `app/agent/factory.py`, thread `fast_path` through:

```python
def build_agent(settings, ctx, checkpointer=None, fast_path=None):
    ...
    return create_agent(
        model=llm,
        tools=tools,
        middleware=build_middleware(settings, guard, base_prompt, budget, fast_path=fast_path),
        checkpointer=checkpointer or BoundedMemorySaver(),
    )
```

Rewrite `app/needle/factory.py` to return the backend + menu provider (no router):

```python
"""Build the fast-path backend + menu provider from settings, or None when off."""
from __future__ import annotations
import logging
from app.constants import AI_AUTOMATION_PREFIX_ACTION
from app.needle.menu import MenuProvider
from app.needle.remote_backend import RemoteNeedleBackend
from app.tools import registry

log = logging.getLogger("fast_path")

def build_fast_path_backend(cfg, rest, ctx):
    if not cfg.needle_enabled or cfg.max_tier < 2:
        log.info("fast path off (needle_enabled=%s max_tier=%s)", cfg.needle_enabled, cfg.max_tier)
        return None
    if not cfg.needle_remote_url:
        log.warning("needle enabled but needle_remote_url is empty; fast path off")
        return None
    if registry.get("trigger_automation") is None:
        log.warning("needle enabled but trigger_automation not registered; fast path off")
        return None
    backend = RemoteNeedleBackend(cfg.needle_remote_url)
    menu_provider = MenuProvider(rest, ttl_s=cfg.needle_menu_ttl_s,
                                 prefix=AI_AUTOMATION_PREFIX_ACTION, ws=ctx.ws)
    return backend, menu_provider
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fast_path_wiring.py tests/ -q -k "middleware or wiring or factory"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/agent/middleware/__init__.py app/agent/factory.py app/needle/factory.py tests/test_fast_path_wiring.py
git commit -m "feat: wire FastPathMiddleware into the agent build"
```

---

## Task 4: Remove fast-path branches from `main.py`; end-to-end hit test

**Files:**
- Modify: `app/main.py`
- Delete: `app/needle/router.py`, `tests/test_needle_router.py` (its target, `FastPathRouter`, is gone)
- Test: `tests/test_fast_path_endpoint.py`

**Interfaces:**
- Consumes: `build_fast_path_backend` (Task 3), `build_agent(..., fast_path=...)`.
- Produces: `main.py` builds `fast_path = build_fast_path_backend(cfg, rest, ctx)` in lifespan and passes it to `build_agent`; both endpoints no longer reference `app.state.fast_path` or `try_fast_path`.

- [ ] **Step 1: Write the failing test**

This test drives the real agent graph (via `build_agent`) with a scripted model that must never run, a `FakeBackend` hit, and a `FakeRest` whose `get_state` returns the AI-actions switch `on` so the tier-2 gate passes, then asserts the persisted thread history. It mirrors `tests/test_e2e_graph.py`.

```python
# tests/test_fast_path_endpoint.py  (flat, per Test Conventions)
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent import factory as factory_mod
from app.config import Settings
from app.fast_path.backend import Decision, FakeBackend
from app.needle.menu import Menu, MenuItem
from app.tools import registry
from app.tools.context import ToolContext

_MENU = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="s")

class _Menu:
    async def get(self): return _MENU

class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

class FakeRest:
    async def get_state(self, entity_id):  # AI-actions switch reads on → gate passes
        return {"entity_id": entity_id, "state": "on", "attributes": {}}
    async def list_states(self):
        return []

async def test_fast_path_hit_persists_full_turn(monkeypatch):
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=2)  # tier-2 → trigger_automation registered
    ctx = ToolContext(settings=settings, rest=FakeRest(), ws=None)
    # Scripted with an entry that must NOT be consumed (the fast path short-circuits).
    model = ScriptedModel(responses=[AIMessage(content="LLM SHOULD NOT RUN")])
    monkeypatch.setattr(factory_mod, "build_llm", lambda s: model)
    agent = factory_mod.build_agent(
        settings, ctx,
        fast_path=(FakeBackend(Decision("automation.ai_action_night", 0.9)), _Menu()),
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "goodnight"}]},
        config={"configurable": {"thread_id": "t1"}, "recursion_limit": 15},
    )
    msgs = result["messages"]
    assert isinstance(msgs[0], HumanMessage)
    assert isinstance(msgs[1], AIMessage) and msgs[1].tool_calls
    assert isinstance(msgs[2], ToolMessage)
    assert isinstance(msgs[3], AIMessage)
    assert "automation.ai_action_night" in msgs[3].content
    assert msgs[3].content != "LLM SHOULD NOT RUN"
    registry._reset_for_tests()
```

> The implementer must confirm here whether the `fastpath_entity_id` kwarg survives onto `msgs[2]` (the `ToolMessage`). If it does not, switch `FastPathMiddleware`'s follow-up branch to read the entity id from `messages[-2].tool_calls[0]["args"]["entity_id"]` (see Task 2's note) and re-run.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fast_path_endpoint.py -v`
Expected: FAIL — until `build_agent` is invoked with `fast_path` and the middleware persists the turn (and possibly the entity-id-source fix).

- [ ] **Step 3: Write minimal implementation**

In `app/main.py` lifespan, replace the `app.state.fast_path` block:

```python
from app.needle.factory import build_fast_path_backend
...
fast_path = None
try:
    fast_path = build_fast_path_backend(cfg, rest, ctx)
except Exception:
    log.exception("fast path failed to build; running agent-only")
app.state.agent = build_agent(cfg, ctx, checkpointer=checkpointer, fast_path=fast_path)
```

Remove `app.state.fast_path = None` and the two endpoint branches that call `router.try_fast_path(...)` — `/api/chat` and `/api/chat/stream` now go straight to the agent / `stream_events`. Delete the `from app.needle.factory import build_fast_path_router` import. Delete `app/needle/router.py`. Apply the entity-id-source fix in `FastPathMiddleware` if the integration test requires it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ -q`
Expected: PASS; no new warnings; no remaining reference to `try_fast_path` (`grep -rn try_fast_path app tests` returns nothing).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_fast_path_endpoint.py
git rm app/needle/router.py
git commit -m "refactor: route fast path through the agent; remove FastPathRouter"
```

---

## Task 5: Streaming fix — accept complete `AIMessage`

**Files:**
- Modify: `app/agent/streaming.py:10,94`
- Test: `tests/test_fast_path_streaming.py`

**Interfaces:**
- Produces: `stream_events` emits the fast-path tool call, tool result, and templated reply from complete `AIMessage`/`ToolMessage` objects, and does not duplicate a normally streamed reply.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fast_path_streaming.py
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

class _Agent:
    def __init__(self, items): self._items = items
    async def astream(self, *a, **k):
        for it in self._items:
            yield it

@pytest.mark.asyncio
async def test_stream_emits_complete_aimessage():
    from app.agent.streaming import stream_events
    full = AIMessage(content="Done — triggered automation.ai_action_night.")
    agent = _Agent([(full, {})])
    events = [e async for e in stream_events(agent, "goodnight", "t1", 15)]
    texts = "".join(e["text"] for e in events if e["type"] == "token")
    assert "automation.ai_action_night" in texts
    assert events[-1]["type"] == "done"

@pytest.mark.asyncio
async def test_stream_does_not_duplicate_chunks():
    from app.agent.streaming import stream_events
    chunks = [(AIMessageChunk(content="Hel"), {}), (AIMessageChunk(content="lo"), {})]
    agent = _Agent(chunks)
    events = [e async for e in stream_events(agent, "hi", "t1", 15)]
    assert events[-1]["reply"] == "Hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fast_path_streaming.py -v`
Expected: FAIL on `test_stream_emits_complete_aimessage` — the full `AIMessage` is skipped by the `AIMessageChunk` check; reply is empty.

- [ ] **Step 3: Write minimal implementation**

In `app/agent/streaming.py`: add `AIMessage` to the import on line 10 (`from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage`) and change line 94 `if isinstance(msg, AIMessageChunk):` to `if isinstance(msg, AIMessage):` (`AIMessageChunk` subclasses `AIMessage`, so streamed chunks still match).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fast_path_streaming.py tests/ -q -k stream`
Expected: PASS.

- [ ] **Step 5: Commit + confirm against llama.cpp**

```bash
git add app/agent/streaming.py tests/test_fast_path_streaming.py
git commit -m "fix: stream complete AIMessage so fast-path replies are not dropped"
```

Manual check (not automated): run `uv run uvicorn app.main:create_app --factory --port 8099` against a real llama.cpp provider, send a normal streamed request, and confirm the reply is not duplicated (spec risk: "llama.cpp streaming duplication").

---

# Phase 0b — Dataset & instrumentation

## Task 6: `app/telemetry/thinking.py` — `split_thinking`

**Files:**
- Create: `app/telemetry/__init__.py` (empty), `app/telemetry/thinking.py`
- Test: `tests/test_telemetry_thinking.py`

**Interfaces:**
- Produces: `split_thinking(message) -> tuple[str, str]` returning `(thinking, content)`; module constants `THINK_OPEN = "<think>"`, `THINK_CLOSE = "</think>"` (shared with `streaming.py`'s `_ThinkBuffer`). Reads `additional_kwargs["reasoning_content"]` (Ollama); else extracts a `<think>…</think>` block from string content (llama.cpp); else `("", content)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_thinking.py
from langchain_core.messages import AIMessage
from app.telemetry.thinking import split_thinking

def test_reasoning_content_field():
    m = AIMessage(content="the answer is 5", additional_kwargs={"reasoning_content": "let me think"})
    assert split_thinking(m) == ("let me think", "the answer is 5")

def test_inline_think_block():
    m = AIMessage(content="<think>hmm</think>the answer is 5")
    assert split_thinking(m) == ("hmm", "the answer is 5")

def test_no_thinking():
    m = AIMessage(content="plain")
    assert split_thinking(m) == ("", "plain")

def test_list_content_flattened():
    m = AIMessage(content=[{"type": "text", "text": "hello"}])
    assert split_thinking(m) == ("", "hello")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_thinking.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# app/telemetry/thinking.py
"""Non-streaming counterpart of streaming._ThinkBuffer: split a complete message
into (thinking, visible content)."""
from __future__ import annotations

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "thinking":
                    continue
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def split_thinking(message) -> tuple[str, str]:
    reasoning = message.additional_kwargs.get("reasoning_content", "")
    content = _flatten(message.content)
    if reasoning:
        return reasoning, content
    open_at = content.find(THINK_OPEN)
    close_at = content.find(THINK_CLOSE)
    if open_at != -1 and close_at != -1 and close_at > open_at:
        thinking = content[open_at + len(THINK_OPEN):close_at]
        visible = content[:open_at] + content[close_at + len(THINK_CLOSE):]
        return thinking, visible
    return "", content
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_thinking.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/__init__.py app/telemetry/thinking.py tests/test_telemetry_thinking.py
git commit -m "feat: add split_thinking for non-streaming telemetry"
```

---

## Task 7: `app/telemetry/conventions.py` — span names & attribute keys

**Files:**
- Create: `app/telemetry/conventions.py`
- Test: `tests/test_telemetry_conventions.py`

**Interfaces:**
- Produces: string constants for span names (`SPAN_INVOKE_AGENT="invoke_agent gosling"`, `SPAN_CHAT="chat"`, `SPAN_CLASSIFY="fast_path.classify"`, `SPAN_EXECUTE_TOOL="execute_tool"`) and attribute keys used by the middleware and exporter. Single source of truth; the exporter reads spans by these keys.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_conventions.py
from app.telemetry import conventions as c

def test_keys_are_pinned_strings():
    assert c.SPAN_INVOKE_AGENT == "invoke_agent gosling"
    assert c.GEN_AI_OPERATION_NAME == "gen_ai.operation.name"
    assert c.GOSLING_FAST_PATH == "gosling.fast_path"
    assert c.GOSLING_TOOLS_OFFERED == "gosling.tools.offered"
    assert c.GOSLING_PATH == "gosling.path"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_conventions.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Create `app/telemetry/conventions.py` with the constants named in the spec's span-model section. Include at minimum: span names above; `GEN_AI_OPERATION_NAME`, `GEN_AI_CONVERSATION_ID`, `GEN_AI_REQUEST_MODEL`, `GEN_AI_RESPONSE_MODEL`, `GEN_AI_USAGE_INPUT_TOKENS`, `GEN_AI_USAGE_OUTPUT_TOKENS`, `GEN_AI_RESPONSE_FINISH_REASONS`, `GEN_AI_TOOL_NAME`, `GEN_AI_TOOL_CALL_ID`; and the `gosling.*` keys: `GOSLING_CHANNEL`, `GOSLING_ENDPOINT`, `GOSLING_DEVICE_ID`, `GOSLING_INPUT_TEXT`, `GOSLING_OUTPUT_TEXT`, `GOSLING_PATH`, `GOSLING_OUTCOME`, `GOSLING_PROMPT_HASH`, `GOSLING_TOOLSET_HASH`, `GOSLING_MAX_TIER`, `GOSLING_APP_VERSION`, `GOSLING_TTFT_MS`, `GOSLING_FAST_PATH`, `GOSLING_STEP`, `GOSLING_TOOLS_OFFERED`, `GOSLING_MESSAGES_COUNT`, `GOSLING_THINKING_TEXT`, `GOSLING_CONTENT_TEXT`, `GOSLING_TOOL_CALLS`, `GOSLING_FP_BACKEND`, `GOSLING_FP_MENU_HASH`, `GOSLING_FP_ENTITY_ID`, `GOSLING_FP_CONFIDENCE`, `GOSLING_FP_THRESHOLD`, `GOSLING_FP_ACCEPTED`, `GOSLING_FP_SKIP_REASON`, `GOSLING_TOOL_ARGS`, `GOSLING_TOOL_STATUS`, `GOSLING_TOOL_ERROR_CODE`, `GOSLING_TOOL_RESULT`. Values are the dotted strings from the spec tables (e.g. `GOSLING_FAST_PATH = "gosling.fast_path"`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_conventions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/conventions.py tests/test_telemetry_conventions.py
git commit -m "feat: pin telemetry span names and attribute keys"
```

---

## Task 8: `app/telemetry/store.py` — schema, migrations, writer connection

**Files:**
- Create: `app/telemetry/store.py`
- Test: `tests/test_telemetry_store_schema.py`

**Interfaces:**
- Produces: `open_store(path: str) -> sqlite3.Connection` (WAL, foreign keys on, migrations applied); `SCHEMA_VERSION: int`; `apply_migrations(conn)`. Tables per the spec: `schema_version`, `requests`, `model_calls`, `tool_calls`, `fast_path_decisions`, `labels`, `prompt_snapshots`, `toolset_snapshots`, `menu_snapshots`, plus the indexes. Full DDL is in the detailed design doc (`.../plans/2026-09-17-agent-telemetry-design.md`, "Dataset schema").

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_store_schema.py
from app.telemetry.store import open_store, SCHEMA_VERSION

def test_open_store_creates_all_tables(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"requests", "model_calls", "tool_calls", "fast_path_decisions",
            "labels", "prompt_snapshots", "toolset_snapshots", "menu_snapshots"} <= names
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION

def test_migrations_are_idempotent(tmp_path):
    p = str(tmp_path / "t.sqlite")
    open_store(p).close()
    conn = open_store(p)  # second open must not fail or duplicate
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1

def test_wal_mode(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_store_schema.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Create `app/telemetry/store.py` with `SCHEMA_VERSION = 1`, `_MIGRATIONS` a list of SQL strings (migration 1 = the full DDL from the spec, verbatim, including the `CREATE INDEX` statements and the `labels` table), and:

```python
import sqlite3

def open_store(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    apply_migrations(conn)
    return conn

def apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row[0] if row else 0
    for i, sql in enumerate(_MIGRATIONS, start=1):
        if i > current:
            conn.executescript(sql)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
```

Copy the `CREATE TABLE`/`CREATE INDEX` statements from the spec's Dataset schema section into `_MIGRATIONS[0]` verbatim (do not include the `schema_version` CREATE there — it is handled above).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_store_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/store.py tests/test_telemetry_store_schema.py
git commit -m "feat: telemetry sqlite store schema and migrations"
```

---

## Task 9: `app/telemetry/snapshots.py` — hashing & registration

**Files:**
- Create: `app/telemetry/snapshots.py`
- Test: `tests/test_telemetry_snapshots.py`

**Interfaces:**
- Consumes: `open_store` (Task 8); `Menu`/`MenuItem` (`app/needle/menu.py`).
- Produces: `content_hash(obj) -> str` (SHA-256 over canonical JSON, 16 hex chars); `prompt_hash(base_prompt) -> str`; `toolset_hash(tools) -> str` over `[{name, description, args_schema}]` sorted by name (accepts LangChain `StructuredTool`s); `menu_hash(menu) -> str` over `[{entity_id, name, description}]` as sent to the backend; `register_prompt(conn, base_prompt)`, `register_toolset(conn, tools)`, `register_menu(conn, menu)` each write once per hash into the matching `*_snapshots` table and return the hash.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_snapshots.py
from app.telemetry.store import open_store
from app.telemetry.snapshots import menu_hash, register_menu, content_hash
from app.needle.menu import Menu, MenuItem

def test_menu_hash_changes_on_description_but_signature_would_not():
    m1 = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="sig")
    m2 = Menu(items=(MenuItem("automation.ai_action_night", "Night", "sleep well"),), signature="sig")
    assert menu_hash(m1) != menu_hash(m2)          # description is part of the hash
    assert m1.signature == m2.signature            # signature is entity-id only

def test_hash_is_16_hex():
    h = content_hash({"a": 1})
    assert len(h) == 16 and all(ch in "0123456789abcdef" for ch in h)

def test_register_menu_writes_once(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    m = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="sig")
    h1 = register_menu(conn, m)
    h2 = register_menu(conn, m)
    assert h1 == h2
    assert conn.execute("SELECT COUNT(*) FROM menu_snapshots WHERE hash=?", (h1,)).fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_snapshots.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# app/telemetry/snapshots.py
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timezone


def content_hash(obj) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def prompt_hash(base_prompt: str) -> str:
    return content_hash(base_prompt)


def _toolset_shape(tools) -> list:
    shaped = []
    for t in tools:
        schema = t.args_schema
        try:
            schema_repr = schema.model_json_schema()
        except AttributeError:
            schema_repr = str(schema)
        shaped.append({"name": t.name, "description": t.description, "args_schema": schema_repr})
    shaped.sort(key=lambda d: d["name"])
    return shaped


def toolset_hash(tools) -> str:
    return content_hash(_toolset_shape(tools))


def _menu_shape(menu) -> list:
    shaped = []
    for item in menu.items:
        shaped.append({"entity_id": item.entity_id, "name": item.name, "description": item.description})
    return shaped


def menu_hash(menu) -> str:
    return content_hash(_menu_shape(menu))


def _register(conn, table, column, hash_value, payload):
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE hash=?", (hash_value,)).fetchone()
    if exists is None:
        conn.execute(f"INSERT INTO {table} (hash, {column}, first_seen) VALUES (?, ?, ?)",
                     (hash_value, payload, _now()))
        conn.commit()
    return hash_value


def register_prompt(conn, base_prompt: str) -> str:
    return _register(conn, "prompt_snapshots", "text", prompt_hash(base_prompt), base_prompt)


def register_toolset(conn, tools) -> str:
    shape = _toolset_shape(tools)
    return _register(conn, "toolset_snapshots", "schemas_json", content_hash(shape),
                     json.dumps(shape, separators=(",", ":")))


def register_menu(conn, menu) -> str:
    shape = _menu_shape(menu)
    return _register(conn, "menu_snapshots", "items_json", content_hash(shape),
                     json.dumps(shape, separators=(",", ":")))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_snapshots.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/snapshots.py tests/test_telemetry_snapshots.py
git commit -m "feat: telemetry snapshot hashing and registration"
```

---

## Task 10: `app/telemetry/sqlite_exporter.py` — spans → rows

**Files:**
- Create: `app/telemetry/sqlite_exporter.py`
- Test: `tests/test_telemetry_sqlite_exporter.py`

**Interfaces:**
- Consumes: `open_store` (Task 8), `conventions` (Task 7); OTel `SpanExporter`, `ReadableSpan`, `SpanExportResult`.
- Produces: `SqliteSpanExporter(conn)` implementing `export(spans) -> SpanExportResult` and `shutdown()`. Maps each span by its name to the matching table: `invoke_agent gosling` → `requests`, `chat *` → `model_calls`, `fast_path.classify` → `fast_path_decisions`, `execute_tool *` → `tool_calls`. `trace_id`/`span_id` are lowercase hex. The tool-result envelope is capped at 64 KB. Any per-span mapping error is logged and skipped (fail open); `export` returns `SUCCESS` unless the DB write itself fails.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_sqlite_exporter.py
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind

from app.telemetry.store import open_store
from app.telemetry.sqlite_exporter import SqliteSpanExporter
from app.telemetry import conventions as c

def _provider(conn):
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(SqliteSpanExporter(conn)))
    return tp

def test_chat_span_maps_to_model_calls_row(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    tp = _provider(conn)
    tracer = tp.get_tracer("test")
    with tracer.start_as_current_span(c.SPAN_INVOKE_AGENT) as root:
        root.set_attribute(c.GOSLING_INPUT_TEXT, "goodnight")
        root.set_attribute(c.GOSLING_PATH, "fast_path")
        root.set_attribute(c.GOSLING_OUTCOME, "ok")
        with tracer.start_as_current_span(f"{c.SPAN_CHAT} needle") as chat:
            chat.set_attribute(c.GOSLING_FAST_PATH, True)
            chat.set_attribute(c.GOSLING_STEP, 1)
            chat.set_attribute(c.GOSLING_TOOLS_OFFERED, '["trigger_automation"]')
    tp.shutdown()
    rows = conn.execute("SELECT fast_path, step, tools_offered FROM model_calls").fetchall()
    assert rows == [(1, 1, '["trigger_automation"]')]
    reqs = conn.execute("SELECT input_text, path, outcome FROM requests").fetchall()
    assert reqs == [("goodnight", "fast_path", "ok")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_sqlite_exporter.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Implement `SqliteSpanExporter(SpanExporter)`. For each span: derive `trace_id = format(span.context.trace_id, "032x")`, `span_id = format(span.context.span_id, "016x")`, `duration_ms` from `(end_time - start_time) / 1e6`, `ts_start` from `start_time` as UTC ISO-8601. Dispatch on `span.name` (`startswith` for `chat `/`execute_tool `, exact for the root and classify). Read attributes by the `conventions` keys with `span.attributes.get(...)`. Insert with `INSERT OR REPLACE` keyed on the primary key so a re-exported span is idempotent. For `requests`, the root maps `request_id = trace_id`. For `tool_calls`, cap `GOSLING_TOOL_RESULT` to 65536 chars before insert. Wrap each span's mapping in `try/except` that logs under `logging.getLogger("telemetry")` and continues. Return `SpanExportResult.SUCCESS`. Full column lists come from Task 8's DDL.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_sqlite_exporter.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/sqlite_exporter.py tests/test_telemetry_sqlite_exporter.py
git commit -m "feat: sqlite span exporter mapping spans to dataset rows"
```

---

## Task 11: `app/telemetry/setup.py` — provider lifecycle

**Files:**
- Create: `app/telemetry/setup.py`
- Test: `tests/test_telemetry_setup.py`

**Interfaces:**
- Consumes: `SqliteSpanExporter` (Task 10), `open_store` (Task 8); OTel `TracerProvider`, `BatchSpanProcessor`.
- Produces: `init_telemetry(settings) -> TracerProvider | None` — returns `None` when `telemetry_enabled` is false or `telemetry_db_path` is empty, or when the DB cannot be opened (logs once, disables). One `BatchSpanProcessor` **per exporter**. `shutdown_telemetry(provider)` flushes with a bounded timeout. `get_tracer(name="gosling")` returns a tracer from the global provider (no-op tracer when uninitialised).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_setup.py
from app.config import Settings
from app.telemetry.setup import init_telemetry, shutdown_telemetry

def test_disabled_returns_none():
    assert init_telemetry(Settings(telemetry_enabled=False)) is None

def test_unwritable_db_disables_but_does_not_raise():
    s = Settings(telemetry_enabled=True, telemetry_db_path="/does/not/exist/telemetry.sqlite")
    provider = init_telemetry(s)   # logs once, returns None
    assert provider is None

def test_enabled_returns_provider(tmp_path):
    s = Settings(telemetry_enabled=True, telemetry_db_path=str(tmp_path / "t.sqlite"))
    provider = init_telemetry(s)
    assert provider is not None
    shutdown_telemetry(provider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_setup.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Implement `init_telemetry`: if disabled/empty path → return `None`. Try `open_store(path)`; on `Exception` log once under `telemetry` and return `None`. Build a `TracerProvider(resource=Resource.create({"service.name": "gosling"}))`, add `BatchSpanProcessor(SqliteSpanExporter(conn))`, `trace.set_tracer_provider(provider)`, return it. `get_tracer` returns `trace.get_tracer("gosling")`. `shutdown_telemetry(provider)` calls `provider.shutdown()` inside `try/except`. (OTLP is Phase 2 — not added here.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_setup.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/setup.py tests/test_telemetry_setup.py
git commit -m "feat: telemetry provider init/shutdown with sqlite exporter"
```

---

## Task 12: `TelemetryMiddleware`

**Files:**
- Create: `app/telemetry/middleware.py`
- Modify: `app/agent/middleware/__init__.py`
- Test: `tests/test_telemetry_middleware.py`

**Interfaces:**
- Consumes: `get_tracer` (Task 11), `conventions` (Task 7), `split_thinking` (Task 6); LangChain `AgentMiddleware`.
- Produces: `TelemetryMiddleware(tracer)`. `awrap_model_call` opens a `chat <model>` span, sets offered tools (`[t.name for t in request.tools]` as JSON), message count, and step index (from a module contextvar `current_step`), `await handler(request)`, then reads the returned message for model name + fast-path flag (`response_metadata`), thinking/content (`split_thinking`), token usage (`usage_metadata`), tool calls; ends the span. `awrap_tool_call` opens an `execute_tool <name>` span, runs the handler, parses the JSON envelope for `status`/`error_code`, ends the span. Every attribute read is wrapped so a telemetry exception never changes the handler result. `build_middleware` gains a `telemetry_tracer=None` param and inserts `TelemetryMiddleware` immediately before `FastPathMiddleware` (i.e. outside it, inside everything else) when a tracer is given.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_middleware.py
import json
import pytest
from types import SimpleNamespace
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.telemetry.middleware import TelemetryMiddleware, current_step

def _tracer_and_spans():
    exp = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exp))
    return tp.get_tracer("test"), exp

@pytest.mark.asyncio
async def test_model_call_records_offered_tools_and_content():
    tracer, exp = _tracer_and_spans()
    mw = TelemetryMiddleware(tracer)
    current_step.set(1)
    req = SimpleNamespace(messages=[], tools=[SimpleNamespace(name="get_weather")])
    async def handler(request):
        return AIMessage(content="sunny", response_metadata={"model_name": "qwen"})
    await mw.awrap_model_call(req, handler)
    span = exp.get_finished_spans()[0]
    assert span.attributes["gosling.tools.offered"] == '["get_weather"]'
    assert span.attributes["gosling.content.text"] == "sunny"
    assert span.attributes["gen_ai.response.model"] == "qwen"

@pytest.mark.asyncio
async def test_telemetry_exception_does_not_break_handler():
    tracer, exp = _tracer_and_spans()
    mw = TelemetryMiddleware(tracer)
    req = SimpleNamespace(messages=[], tools=[SimpleNamespace()])  # .name missing → attr error
    async def handler(request):
        return AIMessage(content="ok")
    resp = await mw.awrap_model_call(req, handler)
    assert resp.content == "ok"

@pytest.mark.asyncio
async def test_tool_call_parses_status():
    tracer, exp = _tracer_and_spans()
    mw = TelemetryMiddleware(tracer)
    req = SimpleNamespace(tool_call={"name": "trigger_automation", "id": "fastpath-1", "args": {}})
    async def handler(request):
        return SimpleNamespace(content=json.dumps({"status": "error", "error": {"code": "ai_disabled"}}))
    await mw.awrap_tool_call(req, handler)
    span = [s for s in exp.get_finished_spans() if s.name.startswith("execute_tool")][0]
    assert span.attributes["gosling.tool.status"] == "error"
    assert span.attributes["gosling.tool.error_code"] == "ai_disabled"
```

> Note: confirm the real shape of the `awrap_tool_call` request against the venv (`inspect` `ToolCallRequest` — it exposes the tool call dict/name/args and the result message). Adjust the attribute reads to the real field names; the test's `SimpleNamespace` mirrors the confirmed shape.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_middleware.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Implement `TelemetryMiddleware` with a module-level `current_step = contextvars.ContextVar("gosling_step", default=0)`. In `awrap_model_call`, `with self._tracer.start_as_current_span(...) as span:` set offered tools / message count / step (each read wrapped in a helper `_safe_set(span, key, fn)` that swallows exceptions), call `await handler(request)`, then `_safe_set` the response-derived attributes via `split_thinking` and `response_metadata`/`usage_metadata`/`tool_calls`, and return the handler result. In `awrap_tool_call`, open `execute_tool <name>`, run handler, parse `json.loads(result.content)` for `status`/`error.code`, cap the raw content at 64 KB for `GOSLING_TOOL_RESULT`, return the result. Verify the `ToolCallRequest` field names against the venv first.

**Wiring the tracer through the build.** Thread telemetry into the agent so both `TelemetryMiddleware` and (in Task 15) `FastPathMiddleware` receive it:

- `build_middleware(...)` gains `telemetry_tracer=None`. When set, append `TelemetryMiddleware(telemetry_tracer)` **after** `HistoryCapMiddleware` and **before** the `FastPathMiddleware` append, so Telemetry is outer to FastPath and inner to everything else (matches the spec's stack order).
- `build_agent(settings, ctx, checkpointer=None, fast_path=None, telemetry=None)` gains `telemetry`, a small tuple `(tracer, store_conn)` or `None`. It unpacks the tracer and passes it as `telemetry_tracer` to `build_middleware` (Task 15 also forwards `store_conn` into the `FastPathMiddleware` construction).
- `main.py` builds `telemetry = (get_tracer(), store_conn)` from the initialised provider (Task 13) and passes it to `build_agent`. When telemetry is disabled, `telemetry=None` and no `TelemetryMiddleware` is added.

Add a `build_middleware` ordering test asserting, with a dummy tracer, that `TelemetryMiddleware` sits immediately outside `FastPathMiddleware` (index of Telemetry == index of FastPath − 1).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_middleware.py tests/test_fast_path_wiring.py -v`
Expected: PASS (wiring test still green — Telemetry sits outside FastPath).

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/middleware.py app/agent/middleware/__init__.py tests/test_telemetry_middleware.py
git commit -m "feat: TelemetryMiddleware emits chat and execute_tool spans"
```

---

## Task 13: Request root span, `request_id` in responses, snapshots & provider lifecycle

**Files:**
- Modify: `app/main.py`, `app/agent/factory.py`, `app/cli.py`, `app/config.py`
- Test: `tests/test_telemetry_request_span.py`

**Interfaces:**
- Consumes: `init_telemetry`/`shutdown_telemetry`/`get_tracer` (Task 11), `conventions` (Task 7), `register_prompt`/`register_toolset`/`register_menu` (Task 9).
- Produces: lifespan calls `init_telemetry(cfg)` and stores the provider on `app.state`; `shutdown_telemetry` in teardown. Both chat endpoints open an `invoke_agent gosling` span around the agent call, set channel/endpoint/input/output/path/outcome, and return `request_id = trace_id` — `ChatResponse` gains `request_id: str | None = None`; SSE `done`/`error` events gain `request_id`. `path` is read from a `fast_path_seen` contextvar the `chat` span sets on step 1. `build_agent` registers prompt + toolset snapshots when a telemetry provider/conn is available.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_request_span.py
from app.main import ChatResponse

def test_chatresponse_has_optional_request_id():
    assert ChatResponse(reply="hi").request_id is None
    assert ChatResponse(reply="hi", request_id="abc").request_id == "abc"
```

Add an endpoint-level test with the existing `TestClient` fixture asserting the `/api/chat` JSON body contains a non-null `request_id` when telemetry is enabled with a temp DB (mirror the closest existing endpoint test for app construction).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_request_span.py -v`
Expected: FAIL — `ChatResponse` has no `request_id`.

- [ ] **Step 3: Write minimal implementation**

Add `request_id: str | None = None` to `ChatResponse`. In the lifespan: `provider = init_telemetry(cfg)`; store `app.state.telemetry = provider`; register prompt/toolset snapshots after `build_agent` if the store is reachable; `shutdown_telemetry(provider)` in `_teardown`. Wrap the `/api/chat` agent call in `with get_tracer().start_as_current_span(conventions.SPAN_INVOKE_AGENT) as span:` — set input text, endpoint `/api/chat`, channel `assist`, `request_id = format(span.context.trace_id, "032x")`, and on completion set output text, `gosling.path` (from the `fast_path_seen` contextvar), `gosling.outcome`; return `ChatResponse(reply=..., request_id=request_id)`. Mirror for `/api/chat/stream`, ending the span in a `finally` inside the SSE generator, mapping client disconnect to `outcome=cancelled`, adding `request_id` to the `done`/`error` events, and channel `ui`. Add the `fast_path_seen` contextvar to `app/telemetry/middleware.py` and have the `chat` span set it on step 1 from the `gosling.fast_path` flag. Add the `channel` derivation (`/api/chat`→`assist`, `/api/chat/stream`→`ui`, CLI→`cli`), with an optional `channel` field on `ChatRequest` (`ChatRequest` already ignores unknown fields).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ -q`
Expected: PASS; no new warnings.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/agent/factory.py app/cli.py app/config.py tests/test_telemetry_request_span.py
git commit -m "feat: request root span, request_id in responses, snapshot registration"
```

---

## Task 14: Config options, addon schema, deps, privacy

**Files:**
- Modify: `app/config.py`, `config.yaml`, `pyproject.toml`, `app/main.py`
- Test: `tests/test_telemetry_config.py`

**Interfaces:**
- Produces: new `Settings` fields `telemetry_enabled: bool = True`, `telemetry_db_path: str = "/data/telemetry.sqlite"`, `telemetry_retention_days: int = 0`, `otlp_endpoint: str = ""`; identical keys in `config.yaml`'s `options`/`schema`; `opentelemetry-api` + `opentelemetry-sdk` in `pyproject.toml`; a startup step that unsets `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2` and, if `otlp_endpoint` is set, validates it resolves to a private address (refuse otherwise). The `backup_exclude` note for `telemetry.sqlite` is added to `config.yaml`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_config.py
from app.config import Settings

def test_telemetry_defaults():
    s = Settings()
    assert s.telemetry_enabled is True
    assert s.telemetry_db_path == "/data/telemetry.sqlite"
    assert s.telemetry_retention_days == 0
    assert s.otlp_endpoint == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_config.py -v`
Expected: FAIL — fields missing.

- [ ] **Step 3: Write minimal implementation**

Add the four fields to `Settings` (with the comment block from the spec's Configuration table). Add matching entries to `config.yaml` `options:` and `schema:` (types: `bool`, `str?`, `int`, `str?`). Add `opentelemetry-api` and `opentelemetry-sdk` to `pyproject.toml` dependencies and run `uv sync`. In `app/main.py` lifespan, before `init_telemetry`, `os.environ.pop("LANGSMITH_TRACING", None)` / `os.environ.pop("LANGCHAIN_TRACING_V2", None)`; if `cfg.otlp_endpoint` is set, resolve the host and refuse to start if it is not an RFC1918/loopback address (Phase 2 uses this; v1 default empty skips it). Add a `backup_exclude` note for `telemetry.sqlite` in `config.yaml`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_config.py tests/ -q`
Expected: PASS. Also run `uv run python -c "import app.telemetry.setup"` to confirm the OTel deps import.

- [ ] **Step 5: Commit**

```bash
git add app/config.py config.yaml pyproject.toml uv.lock app/main.py tests/test_telemetry_config.py
git commit -m "feat: telemetry config options, deps, and LAN-only guards"
```

---

## Task 15: Fast-path classify span wiring & end-to-end telemetry test

**Files:**
- Modify: `app/fast_path/middleware.py`
- Test: `tests/test_telemetry_fastpath_spans.py`

**Interfaces:**
- Consumes: `get_tracer` (Task 11), `conventions` (Task 7), `menu_hash`/`register_menu` (Task 9).
- Produces: `FastPathMiddleware` opens a `fast_path.classify` child span (backend name, menu hash, entity/confidence/threshold/accepted, or `skip_reason`) around `classify`, and records the menu snapshot. The middleware takes an optional `tracer=None` and `store_conn=None`; when absent it degrades to no telemetry (unit tests from Task 2 still pass).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_fastpath_spans.py
import pytest
from types import SimpleNamespace
from langchain_core.messages import HumanMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.fast_path.backend import Decision, FakeBackend
from app.fast_path.middleware import FastPathMiddleware
from app.needle.menu import Menu, MenuItem

_MENU = Menu(items=(MenuItem("automation.ai_action_night", "Night", "goodnight"),), signature="s")

class _Menu:
    async def get(self): return _MENU

@pytest.mark.asyncio
async def test_classify_span_emitted_on_hit():
    exp = InMemorySpanExporter()
    tp = TracerProvider(); tp.add_span_processor(SimpleSpanProcessor(exp))
    tracer = tp.get_tracer("test")
    mw = FastPathMiddleware(FakeBackend(Decision("automation.ai_action_night", 0.9)),
                            _Menu(), threshold=0.0, tracer=tracer)
    async def handler(request): raise AssertionError("no handler")
    await mw.awrap_model_call(SimpleNamespace(messages=[HumanMessage("goodnight")], tools=[], runtime=None), handler)
    span = [s for s in exp.get_finished_spans() if s.name == "fast_path.classify"][0]
    assert span.attributes["gosling.fast_path.backend"] == "fake"
    assert span.attributes["gosling.fast_path.entity_id"] == "automation.ai_action_night"
    assert span.attributes["gosling.fast_path.accepted"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_fastpath_spans.py -v`
Expected: FAIL — `FastPathMiddleware` has no `tracer` param / no classify span.

- [ ] **Step 3: Write minimal implementation**

Add `tracer=None, store_conn=None` to `FastPathMiddleware.__init__`. In the first-step branch, if `self._tracer` is set, wrap the menu fetch + `classify` in `with self._tracer.start_as_current_span(conventions.SPAN_CLASSIFY) as span:` and set backend/menu-hash/entity/confidence/threshold/accepted attributes, or `gosling.fast_path.skip_reason` on empty menu / error. Register the menu snapshot via `register_menu(self._store_conn, menu)` when `store_conn` is set. Keep all telemetry reads exception-swallowing so a hit still fires if telemetry breaks. Wire the tracer + store into `FastPathMiddleware` construction in `build_middleware` (pass them alongside the existing args).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_fastpath_spans.py tests/test_fast_path_*.py -q`
Expected: PASS (Task 2 unit tests still green with `tracer=None`).

- [ ] **Step 5: Commit**

```bash
git add app/fast_path/middleware.py app/agent/middleware/__init__.py tests/test_telemetry_fastpath_spans.py
git commit -m "feat: fast_path.classify span and menu snapshot recording"
```

---

# Phase 1 — Labels, UI, export

## Task 16: Labels endpoint & store write

**Files:**
- Modify: `app/main.py`, `app/telemetry/store.py`
- Test: `tests/test_telemetry_labels.py`

**Interfaces:**
- Consumes: `open_store` (Task 8).
- Produces: `store.insert_label(conn, request_id, source, rating=None, correct_tool=None, correct_entity_id=None, note=None) -> int`; `POST /api/labels` with body `{request_id, source, rating?, correct_tool?, correct_entity_id?, note?}` (Pydantic `LabelRequest`) → `{"id": <int>}`. Writes only to `labels`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_labels.py
from app.telemetry.store import open_store, insert_label

def test_insert_label(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    conn.execute("INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
                 "VALUES ('r1','2026-09-17T00:00:00Z','ui','hi','agent','ok')")
    conn.commit()
    lid = insert_label(conn, "r1", source="ui", rating=-1, note="wrong")
    row = conn.execute("SELECT request_id, source, rating, note FROM labels WHERE id=?", (lid,)).fetchone()
    assert row == ("r1", "ui", -1, "wrong")
```

Add an endpoint test posting to `/api/labels` via the `TestClient` fixture and asserting a row lands in `labels`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_labels.py -v`
Expected: FAIL — `insert_label` missing.

- [ ] **Step 3: Write minimal implementation**

Add `insert_label` to `store.py` (parameterised `INSERT` with `ts` = UTC now; returns `cursor.lastrowid`). Add `LabelRequest(BaseModel)` and `@app.post("/api/labels")` to `main.py` that opens/uses the store connection on `app.state` and returns `{"id": ...}`. If telemetry is disabled (no store), return HTTP 503.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_labels.py tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/telemetry/store.py tests/test_telemetry_labels.py
git commit -m "feat: labels store write and POST /api/labels"
```

---

## Task 17: Read queries + derived views + telemetry API

**Files:**
- Modify: `app/telemetry/store.py`, `app/main.py`
- Test: `tests/test_telemetry_read_queries.py`

**Interfaces:**
- Produces: view DDL added to migrations (`fast_path_dataset`, `tool_offer_stats`, `request_quality` — definitions in the detailed design doc); `store.summary(conn, days) -> dict` and `store.recent_requests(conn, limit, cursor=None) -> dict`; `GET /api/telemetry/summary?days=7` and `GET /api/telemetry/requests?limit=&cursor=` returning those. Views ship in a new migration (`SCHEMA_VERSION = 2`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_read_queries.py
from app.telemetry.store import open_store, summary

def test_summary_counts_requests_by_path(tmp_path):
    conn = open_store(str(tmp_path / "t.sqlite"))
    for rid, path in [("r1", "fast_path"), ("r2", "agent"), ("r3", "fast_path")]:
        conn.execute("INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
                     "VALUES (?, '2026-09-17T00:00:00Z','ui','hi',?, 'ok')", (rid, path))
    conn.commit()
    s = summary(conn, days=3650)
    assert s["requests_by_path"]["fast_path"] == 2
    assert s["requests_by_path"]["agent"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_read_queries.py -v`
Expected: FAIL — `summary` missing.

- [ ] **Step 3: Write minimal implementation**

Bump `SCHEMA_VERSION = 2`; add `_MIGRATIONS[1]` = the three `CREATE VIEW` statements from the detailed design doc. Implement `summary(conn, days)` (requests/day by path, duration + TTFT percentiles by path, outcome counts, fast-path hit rate, label counts) and `recent_requests(conn, limit, cursor)` (paginated by `ts_start`, with step/thinking/tool drill-down joined from `model_calls`/`tool_calls`). Add the two GET endpoints returning these dicts.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_read_queries.py tests/ -q`
Expected: PASS; migration from v1→v2 works on an existing DB (add a test that opens a v1 store then re-opens).

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/store.py app/main.py tests/test_telemetry_read_queries.py
git commit -m "feat: telemetry read queries, derived views, and summary/requests APIs"
```

---

## Task 18: Metrics tab (frontend)

**Files:**
- Create: `frontend/metrics.html`, `frontend/metrics.js`, `frontend/metrics.css`
- Modify: `frontend/index.html` (tab link), and the chat reply template to add thumbs up/down
- Test: manual (documented below) — no JS test harness in this repo

**Interfaces:**
- Consumes: `GET /api/telemetry/summary`, `GET /api/telemetry/requests`, `POST /api/labels`. Uses relative fetch paths (`api/telemetry/summary`, not `/api/...`) per the ingress-prefix rule in `main.py`.
- Produces: a metrics tab drawing the first-version panels from the spec (requests/day by path; duration & TTFT p50/p95 by path; tool offered/chosen/error table; outcome rates; fast-path hit rate + confidence histogram; label counts + thumbs-down list). Charts use plain SVG/CSS or one vendored library file — no CDN. Chat replies get thumbs up/down posting `source=ui`, with an optional "should have been" picker on thumbs-down.

- [ ] **Step 1: Add the tab and thumbs, wire the fetches**

Create `frontend/metrics.{html,js,css}` following the existing frontend's structure and Catppuccin theme (see `frontend/` and the UI-redesign memory). Fetch `summary` on load and render the panels; render `requests` in a table with a row-expand for steps/thinking/tools. Add thumbs up/down under each chat reply that `POST`s `{request_id, source: "ui", rating}`; on thumbs-down, show a picker (tools, menu automations, "none") that adds `correct_tool`/`correct_entity_id`.

- [ ] **Step 2: Manual verification**

Run `uv run uvicorn app.main:create_app --factory --port 8099` with telemetry enabled and a temp DB seeded by a couple of chat requests. Open the metrics tab; confirm panels render and a thumbs-down writes a `labels` row (`sqlite3 telemetry.sqlite "SELECT * FROM labels"`).

- [ ] **Step 3: Commit**

```bash
git add frontend/metrics.html frontend/metrics.js frontend/metrics.css frontend/index.html
git commit -m "feat: telemetry metrics tab and reply thumbs labelling"
```

---

## Task 19: Export CLI

**Files:**
- Create: `app/telemetry/export.py`
- Test: `tests/test_telemetry_export.py`

**Interfaces:**
- Consumes: `open_store` (Task 8); the `fast_path_dataset` view (Task 17); the eval loader in `tests/evals/run.py`.
- Produces: `python -m app.telemetry.export fast-path [--labelled-only] --out F.jsonl` (one JSONL line per classified request: `{request_id, utterance, menu, prediction, confidence, accepted, label?, agent_reference?}`, menu resolved from `menu_snapshots`), and `python -m app.telemetry.export evals --labelled-only --out F.yaml` (entries in `tests/evals/cases.yaml` format from labelled requests). `argparse` subcommands; `--db` defaults to `telemetry_db_path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_export.py
import json
from app.telemetry.store import open_store
from app.telemetry.export import export_fast_path

def test_export_fast_path_jsonl(tmp_path):
    db = str(tmp_path / "t.sqlite")
    conn = open_store(db)
    conn.execute("INSERT INTO requests (request_id, ts_start, channel, input_text, path, outcome) "
                 "VALUES ('r1','2026-09-17T00:00:00Z','ui','goodnight','fast_path','ok')")
    conn.execute("INSERT INTO menu_snapshots (hash, items_json, first_seen) "
                 "VALUES ('m1', ?, '2026-09-17T00:00:00Z')",
                 (json.dumps([{"entity_id": "automation.ai_action_night", "name": "Night", "description": "goodnight"}]),))
    conn.execute("INSERT INTO fast_path_decisions (request_id, backend, menu_hash, entity_id, confidence, threshold, accepted) "
                 "VALUES ('r1','fake','m1','automation.ai_action_night',0.9,0.0,1)")
    conn.commit(); conn.close()
    out = tmp_path / "fp.jsonl"
    export_fast_path(db, str(out), labelled_only=False)
    line = json.loads(out.read_text().splitlines()[0])
    assert line["utterance"] == "goodnight"
    assert line["prediction"] == "automation.ai_action_night"
    assert line["menu"][0]["entity_id"] == "automation.ai_action_night"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_telemetry_export.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Implement `export_fast_path(db, out, labelled_only)` and `export_evals(db, out, labelled_only)` plus an `argparse` `main()` with `fast-path` / `evals` subcommands. `fast-path` joins `fast_path_decisions` → `requests` → `menu_snapshots`, resolves the menu JSON, and writes one JSON object per line. `evals` selects labelled requests and emits `{id, prompt, expect_tool, expect_params}` YAML entries matching `tests/evals/cases.yaml`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_telemetry_export.py -v`
Then verify the eval output parses: write a tiny labelled fixture, run `export evals`, and load it with `tests/evals/run.py`'s loader (import and call it in the test).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/telemetry/export.py tests/test_telemetry_export.py
git commit -m "feat: telemetry export CLI for fast-path JSONL and eval candidates"
```

---

## Final verification (run before opening the PR)

- [ ] `uv run pytest -q` — full suite green, no new warnings (only the known starlette one).
- [ ] `grep -rn "try_fast_path\|app.state.fast_path\|FastPathRouter" app tests` — returns nothing.
- [ ] `grep -rn "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT" app` — returns nothing (no truncation).
- [ ] Manual smoke: run the server with telemetry on + a tier-2 config + a live/faked Needle, send a "goodnight" request, confirm a `fast_path` request row + two `chat` rows + one `execute_tool` row + one `fast_path_decisions` row, and that a follow-up in the same thread sees the fast-path turn in history.
- [ ] Manual: confirm no duplicate reply against a real llama.cpp streamed response (Task 5).

---

## Self-review notes (author)

- **Spec coverage:** fast-path middleware (T2–4), streaming fix (T5), thinking split (T6), conventions (T7), schema/migrations (T8), snapshots incl. menu-vs-signature (T9), exporter (T10), provider/fail-open (T11), TelemetryMiddleware + ordering (T12), request span + request_id + channel + snapshots (T13), config/deps/privacy (T14), classify span (T15), labels (T16), views + APIs (T17), metrics tab (T18), export (T19). Phases 2–3 are reserved (out of scope) per the spec.
- **Deferred-to-doc detail:** full SQL DDL and the view definitions live in the detailed design doc and are copied verbatim in T8/T17 rather than re-typed here.
- **Two flagged confirmations during execution:** (a) whether `fastpath_entity_id` survives onto the `ToolMessage` or must be read from `messages[-2]` (T2/T4); (b) the exact `ToolCallRequest` field names for `awrap_tool_call` (T12) — both say "verify against the venv" because the installed API is authoritative per the global constraint.
