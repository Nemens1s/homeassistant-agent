# Needle Fast-Path Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional local fast path in front of the LangChain agent that triggers `automation.ai_*` automations in ~1–2 s when a tiny model (Needle 2) is confident, falling through to the existing agent otherwise.

**Architecture:** A new isolated package `app/needle/` with three units — `menu.py` (fetch + cache the `ai_*` automation menu), `backend.py` (swappable inference backend behind a `NeedleBackend` protocol), and `router.py` (`FastPathRouter.try_fast_path`, which decides and, on a confident hit, invokes the *existing* `trigger_automation` `StructuredTool` so the AI-actions gate + audit fire unchanged). Wired into `main.py`'s `/api/chat` ahead of the agent, gated behind `needle_enabled` (default off) and `max_tier >= 2`.

**Tech Stack:** Python 3.14, pydantic / pydantic-settings, FastAPI, LangChain v1 (`StructuredTool`), pytest. Inference runtime: `cactus` (Needle 2 `.cact` binary), in-process.

**Spec:** `docs/superpowers/specs/2026-08-25-needle-fast-path-design.md` — read it alongside this plan.

## Global Constraints

- **Python:** ALWAYS `venv/bin/python` / `venv/bin/pip`. Never system python.
- **Tests:** `venv/bin/python -m pytest -q` must stay fast (no live HA/Ollama/cactus). Exactly 2 known third-party warnings are expected; any new warning is a finding to fix.
- **Plain readable Python:** regular `for` loops over comprehensions; small named helper functions over lambdas/clever one-liners (match `app/tools/registry.py` style).
- **LangChain v1 only** (`StructuredTool`); verify API against the installed venv, don't trust training data.
- **Tool output is always `ToolResult...to_json()` compact JSON** — the router parses that, never `str(dict)`.
- **No import-time singletons.** The router is built in the `main.py` lifespan with injected settings, exactly like `agent`.
- **Menu-only writes stay gated in `adapter.py`.** The router MUST invoke the existing `trigger_automation` `StructuredTool` (`to_structured_tool(...).ainvoke(...)`). It must NEVER call `ctx.rest` or the handler directly — that is the load-bearing safety invariant.
- **Ships disabled.** Router is constructed only when `settings.needle_enabled` is true AND `settings.max_tier >= 2`. With defaults, behavior is byte-identical to today.
- **`AI_AUTOMATION_PREFIX`** (`"automation.ai_"`) is imported from `app/tools/action/trigger_automation.py` — single source of truth, never re-hardcoded.

---

### Task 1: Feasibility spike — cactus in-process (THROWAWAY)

Not a TDD task. Output is a **go/no-go decision (approach A in-process vs approach B sidecar)** plus the exact call sequence for Task 6. Nothing here is kept except a short decision note.

**Files:**
- Create (throwaway, do not commit): `/private/tmp/claude-501/.../scratchpad/needle_spike.py`
- Create (commit): `docs/superpowers/plans/needle-spike-notes.md`

- [ ] **Step 1: Install the runtime into the venv**

Run: `venv/bin/pip install cactus` (or the package name from the Needle 2 model card / repo; check https://github.com/cactus-compute/needle for the exact PyPI name and how to obtain the `.cact` binary). Record the exact package + version installed.

- [ ] **Step 2: Write a scratch probe script**

In the scratchpad, write a minimal script that: (a) loads a Needle 2 `.cact` binary, (b) declares a single tool schema constrained to a small enum of fake ids (e.g. `automation.ai_goodnight`, `automation.ai_movie`), (c) runs one `classify`-style call on `"goodnight"`, (d) prints the chosen id + confidence score.

- [ ] **Step 3: Run it on the target architecture**

Run: `venv/bin/python <scratchpad>/needle_spike.py`
Expected: prints a grammar-constrained id and a float confidence. **Also run it inside the addon container base image if possible** (Sandy Bridge = AVX1, no AVX2; `cactus` is ARM-first — this is the real risk). Capture whether it loads and runs on amd64 without AVX2.

- [ ] **Step 4: Record the decision**

Write `docs/superpowers/plans/needle-spike-notes.md` capturing: package name + version; the exact Python call sequence (load, declare-tools/compile-grammar, classify, read confidence) as a copyable snippet; measured cold-load and per-call latency; and the **decision: approach A (in-process, default) or approach B (sidecar)**. Task 6 copies its vendor glue from this file.

- [ ] **Step 5: Commit the decision note only**

```bash
git add docs/superpowers/plans/needle-spike-notes.md
git commit -m "docs: needle feasibility spike findings + A/B decision"
```

Delete the scratch script; do not commit it.

---

### Task 2: Configuration fields

**Files:**
- Modify: `app/config.py:52-60` (Agent-behavior block of `Settings`)
- Modify: `config.yaml:16-66` (both `options:` defaults and `schema:`)
- Modify: `tests/conftest.py:9-37` (add the new env-var names to the clear list)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.needle_enabled: bool`, `Settings.needle_model_path: str`, `Settings.needle_confidence_threshold: float`, `Settings.needle_menu_ttl_s: int`, `Settings.needle_backend: str`, `Settings.needle_sidecar_url: str`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_needle_defaults_are_off_and_safe():
    s = Settings(_env_file=None)
    assert s.needle_enabled is False
    assert s.needle_confidence_threshold == 0.85
    assert s.needle_menu_ttl_s == 60
    assert s.needle_backend == "cactus"
    assert s.needle_model_path == ""
    assert s.needle_sidecar_url == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_config.py::test_needle_defaults_are_off_and_safe -q`
Expected: FAIL — `AttributeError`/`ValidationError` (fields don't exist).

- [ ] **Step 3: Add the fields**

In `app/config.py`, after the `enable_tool_subsetting` line (~line 60):

```python
    # Needle fast path (optional low-latency automation triggering). Off by
    # default: when enabled AND max_tier >= 2, a local model may trigger an
    # ai_* automation directly, bypassing the agent. See docs spec 2026-08-25.
    needle_enabled: bool = False
    needle_model_path: str = ""          # path to the .cact binary
    needle_confidence_threshold: float = 0.85  # fire only at/above this (>=)
    needle_menu_ttl_s: int = 60          # menu/grammar cache TTL
    needle_backend: str = "cactus"       # "cactus" (in-process) or "sidecar"
    needle_sidecar_url: str = ""         # used only when needle_backend == "sidecar"
```

In `config.yaml`, add to `options:`:

```yaml
  needle_enabled: false
  needle_model_path: ""
  needle_confidence_threshold: 0.85
  needle_menu_ttl_s: 60
  needle_backend: "cactus"
  needle_sidecar_url: ""
```

and to `schema:`:

```yaml
  needle_enabled: "bool"
  needle_model_path: "str"
  needle_confidence_threshold: "float(0,1)"
  needle_menu_ttl_s: "int(1,3600)"
  needle_backend: "list(cactus|sidecar)"
  needle_sidecar_url: "str?"
```

In `tests/conftest.py`, add to `_SETTINGS_ENV_VARS`: `"NEEDLE_ENABLED"`, `"NEEDLE_MODEL_PATH"`, `"NEEDLE_CONFIDENCE_THRESHOLD"`, `"NEEDLE_MENU_TTL_S"`, `"NEEDLE_BACKEND"`, `"NEEDLE_SIDECAR_URL"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py config.yaml tests/conftest.py tests/test_config.py
git commit -m "feat: add needle fast-path settings (disabled by default)"
```

---

### Task 3: Menu provider

**Files:**
- Create: `app/needle/__init__.py` (empty)
- Create: `app/needle/menu.py`
- Test: `tests/test_needle_menu.py`

**Interfaces:**
- Consumes: `ctx.rest.list_states()` → `list[dict]` with `entity_id` and `attributes.friendly_name`; `AI_AUTOMATION_PREFIX` from `app/tools/action/trigger_automation.py`.
- Produces: `MenuItem(entity_id: str, name: str)`; `Menu(items: tuple[MenuItem, ...], signature: str)`; `MenuProvider(rest, ttl_s=60, prefix=AI_AUTOMATION_PREFIX)` with `async get() -> Menu`.

- [ ] **Step 1: Write the failing test**

`tests/test_needle_menu.py`:

```python
from app.needle.menu import Menu, MenuItem, MenuProvider


class FakeRest:
    def __init__(self, states):
        self._states = states
        self.calls = 0

    async def list_states(self):
        self.calls += 1
        return self._states


STATES = [
    {"entity_id": "light.kitchen", "attributes": {}},
    {"entity_id": "automation.ai_goodnight",
     "attributes": {"friendly_name": "Goodnight"}},
    {"entity_id": "automation.morning", "attributes": {"friendly_name": "Morning"}},
    {"entity_id": "automation.ai_movie", "attributes": {"friendly_name": "Movie time"}},
]


async def test_menu_keeps_only_ai_automations_with_names():
    provider = MenuProvider(FakeRest(STATES), ttl_s=60)
    menu = await provider.get()
    ids = []
    for item in menu.items:
        ids.append(item.entity_id)
    assert ids == ["automation.ai_goodnight", "automation.ai_movie"]  # sorted, prefix-filtered
    assert menu.items[0].name == "Goodnight"


async def test_menu_is_cached_within_ttl():
    rest = FakeRest(STATES)
    provider = MenuProvider(rest, ttl_s=60)
    await provider.get()
    await provider.get()
    assert rest.calls == 1  # second call served from cache


async def test_signature_changes_when_id_set_changes():
    a = await MenuProvider(FakeRest(STATES), ttl_s=60).get()
    fewer = [STATES[0], STATES[1]]  # drop ai_movie
    b = await MenuProvider(FakeRest(fewer), ttl_s=60).get()
    assert a.signature != b.signature
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_needle_menu.py -q`
Expected: FAIL — `ModuleNotFoundError: app.needle.menu`.

- [ ] **Step 3: Write the implementation**

`app/needle/__init__.py`: empty file.

`app/needle/menu.py`:

```python
"""The fast-path menu: the set of ai_* automations Needle may trigger, plus a
stable signature of that id set (used by a backend to cache its compiled
grammar). Fetched from HA REST and cached with a short TTL."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from app.tools.action.trigger_automation import AI_AUTOMATION_PREFIX


@dataclass(frozen=True)
class MenuItem:
    entity_id: str
    name: str


@dataclass(frozen=True)
class Menu:
    items: tuple[MenuItem, ...]
    signature: str


def _signature(items: list[MenuItem]) -> str:
    parts = []
    for item in items:
        parts.append(item.entity_id)
    joined = "\n".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _by_id(item: MenuItem) -> str:
    return item.entity_id


class MenuProvider:
    def __init__(self, rest, ttl_s: int = 60, prefix: str = AI_AUTOMATION_PREFIX):
        self._rest = rest
        self._ttl_s = ttl_s
        self._prefix = prefix
        self._cached: Menu | None = None
        self._fetched_at = 0.0

    async def get(self) -> Menu:
        now = time.monotonic()
        if self._cached is not None and (now - self._fetched_at) < self._ttl_s:
            return self._cached
        states = await self._rest.list_states()
        items = []
        for state in states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith(self._prefix):
                continue
            attrs = state.get("attributes") or {}
            name = attrs.get("friendly_name") or entity_id
            items.append(MenuItem(entity_id=entity_id, name=name))
        items.sort(key=_by_id)
        menu = Menu(items=tuple(items), signature=_signature(items))
        self._cached = menu
        self._fetched_at = now
        return menu
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_needle_menu.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/needle/__init__.py app/needle/menu.py tests/test_needle_menu.py
git commit -m "feat: needle menu provider (ai_* automations, cached)"
```

---

### Task 4: Backend protocol, Decision, FakeBackend

**Files:**
- Create: `app/needle/backend.py`
- Test: `tests/test_needle_backend.py`

**Interfaces:**
- Consumes: `Menu` from `app/needle/menu.py`.
- Produces: `Decision(entity_id: str | None, confidence: float)`; `NeedleBackend` Protocol with `async classify(self, message: str, menu: Menu) -> Decision`; `FakeBackend(decision=None, by_message=None)` implementing it.

- [ ] **Step 1: Write the failing test**

`tests/test_needle_backend.py`:

```python
from app.needle.backend import Decision, FakeBackend
from app.needle.menu import Menu, MenuItem

MENU = Menu(items=(MenuItem("automation.ai_goodnight", "Goodnight"),), signature="x")


async def test_fake_backend_returns_scripted_decision():
    backend = FakeBackend(Decision("automation.ai_goodnight", 0.9))
    d = await backend.classify("goodnight", MENU)
    assert d.entity_id == "automation.ai_goodnight"
    assert d.confidence == 0.9


async def test_fake_backend_per_message_lookup_and_default():
    backend = FakeBackend(by_message={"movie": Decision("automation.ai_movie", 0.95)})
    assert (await backend.classify("movie", MENU)).entity_id == "automation.ai_movie"
    fallback = await backend.classify("unknown", MENU)
    assert fallback.entity_id is None and fallback.confidence == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_needle_backend.py -q`
Expected: FAIL — `ModuleNotFoundError: app.needle.backend`.

- [ ] **Step 3: Write the implementation**

`app/needle/backend.py`:

```python
"""Swappable inference backend. The router depends only on the NeedleBackend
protocol; CactusBackend (in-process) is added in a later task. FakeBackend
drives all router unit tests without a real runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.needle.menu import Menu


@dataclass(frozen=True)
class Decision:
    entity_id: str | None   # grammar-constrained to the current menu, or None
    confidence: float


class NeedleBackend(Protocol):
    async def classify(self, message: str, menu: Menu) -> Decision: ...


class FakeBackend:
    """Test backend: returns a per-message Decision if provided, else a single
    scripted Decision, else a no-op (None, 0.0)."""

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

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_needle_backend.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/needle/backend.py tests/test_needle_backend.py
git commit -m "feat: needle backend protocol + Decision + FakeBackend"
```

---

### Task 5: FastPathRouter

**Files:**
- Create: `app/needle/router.py`
- Test: `tests/test_needle_router.py`

**Interfaces:**
- Consumes: a `NeedleBackend`; a `MenuProvider` (anything with `async get() -> Menu`); a `trigger_tool` (LangChain `StructuredTool` whose `ainvoke({"entity_id": ...}, config=...)` returns a `ToolResult` JSON string); a `threshold: float`.
- Produces: `FastPathRouter(backend, menu_provider, trigger_tool, threshold)` with `async try_fast_path(message: str, thread_id: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

`tests/test_needle_router.py`:

```python
import json

from app.config import Settings
from app.needle.backend import Decision, FakeBackend
from app.needle.menu import Menu, MenuItem
from app.needle.router import FastPathRouter
from app.tools import registry
from app.tools.adapter import LoopGuard, to_structured_tool
from app.tools.context import ToolContext

MENU = Menu(items=(MenuItem("automation.ai_goodnight", "Goodnight"),), signature="x")


class FakeMenuProvider:
    def __init__(self, menu):
        self._menu = menu

    async def get(self):
        return self._menu


class RecordingTool:
    """Stand-in trigger tool: records the args and returns a canned envelope."""
    def __init__(self, envelope):
        self.calls = []
        self._envelope = envelope

    async def ainvoke(self, args, config=None):
        self.calls.append((args, config))
        return json.dumps(self._envelope)


def _router(backend, tool, menu=MENU, threshold=0.85):
    return FastPathRouter(backend, FakeMenuProvider(menu), tool, threshold)


async def test_confident_hit_fires_and_returns_reply():
    tool = RecordingTool({"status": "ok", "data": {"entity_id": "automation.ai_goodnight"}})
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.9)), tool)
    reply = await router.try_fast_path("goodnight", "t1")
    assert reply is not None and "automation.ai_goodnight" in reply
    assert tool.calls[0][0] == {"entity_id": "automation.ai_goodnight"}
    assert tool.calls[0][1]["configurable"]["thread_id"] == "t1"


async def test_below_threshold_falls_through():
    tool = RecordingTool({"status": "ok"})
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.5)), tool)
    assert await router.try_fast_path("maybe goodnight", "t1") is None
    assert tool.calls == []  # tool never invoked


async def test_no_decision_falls_through():
    tool = RecordingTool({"status": "ok"})
    router = _router(FakeBackend(Decision(None, 0.99)), tool)
    assert await router.try_fast_path("what's the temperature", "t1") is None
    assert tool.calls == []


async def test_empty_menu_falls_through():
    tool = RecordingTool({"status": "ok"})
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.99)), tool,
                     menu=Menu(items=(), signature="empty"))
    assert await router.try_fast_path("goodnight", "t1") is None
    assert tool.calls == []


async def test_backend_error_falls_through():
    class Boom:
        async def classify(self, message, menu):
            raise RuntimeError("model exploded")

    tool = RecordingTool({"status": "ok"})
    router = _router(Boom(), tool)
    assert await router.try_fast_path("goodnight", "t1") is None
    assert tool.calls == []


async def test_gate_refusal_is_surfaced_not_fallen_through():
    envelope = {"status": "error",
                "error": {"code": "ai_disabled", "message": "AI-triggered actions are off."}}
    tool = RecordingTool(envelope)
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.9)), tool)
    reply = await router.try_fast_path("goodnight", "t1")
    assert reply == "AI-triggered actions are off."   # returned, not None


async def test_confident_hit_goes_through_real_gated_tool(monkeypatch):
    """The real trigger_automation StructuredTool: gate is read, service fired."""
    class FakeRest:
        def __init__(self):
            self.calls = []
        async def get_state(self, entity_id):
            return {"entity_id": entity_id, "state": "on",   # ai_actions_switch ON
                    "attributes": {"last_triggered": "2026-08-25T22:00:00+00:00"}}
        async def call_service(self, domain, service, entity_id):
            self.calls.append((domain, service, entity_id))
            return []

    registry._reset_for_tests()
    registry.load_all(("app.tools.action.trigger_automation",))
    rest = FakeRest()
    ctx = ToolContext(settings=Settings(_env_file=None), rest=rest, ws=None)
    trigger_tool = to_structured_tool(registry.get("trigger_automation"), ctx, LoopGuard())
    router = _router(FakeBackend(Decision("automation.ai_goodnight", 0.9)), trigger_tool)

    reply = await router.try_fast_path("goodnight", "t1")
    assert "automation.ai_goodnight" in reply
    assert ("automation", "trigger", "automation.ai_goodnight") in rest.calls
    registry._reset_for_tests()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_needle_router.py -q`
Expected: FAIL — `ModuleNotFoundError: app.needle.router`.

- [ ] **Step 3: Write the implementation**

`app/needle/router.py`:

```python
"""FastPathRouter: the pre-agent hop. On a confident hit it invokes the EXISTING
trigger_automation StructuredTool (so the AI-actions gate + audit fire exactly as
for the agent) and returns a reply. Otherwise returns None -> caller runs the
agent. Never raises: any failure degrades to the agent."""

from __future__ import annotations

import json
import logging

log = logging.getLogger("needle")


class FastPathRouter:
    def __init__(self, backend, menu_provider, trigger_tool, threshold: float):
        self._backend = backend
        self._menu_provider = menu_provider
        self._trigger_tool = trigger_tool
        self._threshold = threshold

    async def try_fast_path(self, message: str, thread_id: str) -> str | None:
        try:
            menu = await self._menu_provider.get()
            if not menu.items:
                return None
            decision = await self._backend.classify(message, menu)
            if decision.entity_id is None or decision.confidence < self._threshold:
                return None
            raw = await self._trigger_tool.ainvoke(
                {"entity_id": decision.entity_id},
                config={"configurable": {"thread_id": thread_id}},
            )
        except Exception:
            log.exception("needle fast path error; falling through to agent")
            return None
        return _reply_from_envelope(raw, decision.entity_id)


def _reply_from_envelope(raw: str, entity_id: str) -> str | None:
    try:
        env = json.loads(raw)
    except (ValueError, TypeError):
        return None  # malformed (shouldn't happen) -> fall through
    if env.get("status") == "ok":
        return f"Done — triggered {entity_id}."
    error = env.get("error") or {}
    message = error.get("message")
    if message:
        return message
    return "Sorry, I couldn't run that automation."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_needle_router.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/needle/router.py tests/test_needle_router.py
git commit -m "feat: FastPathRouter (confident trigger via gated tool, else fall through)"
```

---

### Task 6: CactusBackend (real runtime, per spike)

**Files:**
- Create: `app/needle/cactus_backend.py`
- Modify: `requirements.txt` (add the cactus package + pinned version from Task 1)
- Modify: `Dockerfile` (ensure the runtime + `.cact` binary are available in the addon image)
- Test: `tests/test_needle_cactus_backend.py` (skipped unless the runtime imports)

**Interfaces:**
- Consumes: `Menu`, `MenuItem` from `app/needle/menu.py`; `Decision`, `NeedleBackend` from `app/needle/backend.py`; call sequence from `docs/superpowers/plans/needle-spike-notes.md`.
- Produces: `CactusBackend(model_path: str)` implementing `NeedleBackend.classify`.

> **Note on vendor glue:** The structure below is fixed. The three marked lines that call the `cactus` API (load, compile-grammar-from-menu, generate+confidence) must be copied verbatim from the Task 1 spike notes — that is the only content this plan cannot pin down before the spike runs. Expected shape per the Needle 2 model card: a tool/function schema whose single argument is an enum of the menu's entity ids (grammar-constrained), and a confidence that is the min of a calibrated head and the decode probability. If the spike chose approach B, implement `SidecarBackend(url)` with the identical `classify` signature instead and skip the Dockerfile model-embedding step.

- [ ] **Step 1: Write the failing (skip-guarded) test**

`tests/test_needle_cactus_backend.py`:

```python
import pytest

cactus = pytest.importorskip("cactus")  # skip entirely if runtime absent

from app.needle.cactus_backend import CactusBackend
from app.needle.menu import Menu, MenuItem

MENU = Menu(items=(MenuItem("automation.ai_goodnight", "Goodnight"),
                   MenuItem("automation.ai_movie", "Movie time")), signature="s1")


async def test_classify_returns_menu_constrained_id_and_confidence():
    backend = CactusBackend(model_path=_TEST_MODEL_PATH)  # from env in conftest
    d = await backend.classify("time for bed", MENU)
    assert d.entity_id in {None, "automation.ai_goodnight", "automation.ai_movie"}
    assert 0.0 <= d.confidence <= 1.0
```

(Set `_TEST_MODEL_PATH` from a `NEEDLE_TEST_MODEL_PATH` env var; skip if unset.)

- [ ] **Step 2: Run test to verify it fails or skips cleanly**

Run: `venv/bin/python -m pytest tests/test_needle_cactus_backend.py -q`
Expected: SKIP if runtime/model absent; FAIL with `ModuleNotFoundError: app.needle.cactus_backend` if the runtime is installed.

- [ ] **Step 3: Write the implementation**

`app/needle/cactus_backend.py`:

```python
"""In-process Needle 2 backend (approach A). Loads the .cact model once and
compiles a byte-level grammar per menu id-set, cached by menu.signature so the
grammar is rebuilt only when the ai_* set changes."""

from __future__ import annotations

from app.needle.backend import Decision
from app.needle.menu import Menu

import cactus  # runtime from the Task 1 spike


class CactusBackend:
    def __init__(self, model_path: str):
        self._model = cactus.load(model_path)   # <-- SPIKE: exact load call
        self._grammar_cache: dict[str, object] = {}

    def _grammar_for(self, menu: Menu):
        cached = self._grammar_cache.get(menu.signature)
        if cached is not None:
            return cached
        ids = []
        for item in menu.items:
            ids.append(item.entity_id)
        grammar = cactus.grammar_from_enum(ids)  # <-- SPIKE: compile grammar from menu ids
        self._grammar_cache[menu.signature] = grammar
        return grammar

    async def classify(self, message: str, menu: Menu) -> Decision:
        grammar = self._grammar_for(menu)
        entity_id, confidence = cactus.classify(  # <-- SPIKE: run + read confidence
            self._model, message, grammar
        )
        return Decision(entity_id=entity_id, confidence=float(confidence))
```

Add the pinned cactus package to `requirements.txt`. Update `Dockerfile` so the runtime and the `.cact` binary ship in the addon image (path matches `needle_model_path`).

- [ ] **Step 4: Run the test / manual smoke on the real model**

Run: `NEEDLE_TEST_MODEL_PATH=<path> venv/bin/python -m pytest tests/test_needle_cactus_backend.py -q`
Expected: PASS when the model is present. Confirm the full suite still shows only the 2 known warnings: `venv/bin/python -m pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add app/needle/cactus_backend.py tests/test_needle_cactus_backend.py requirements.txt Dockerfile
git commit -m "feat: in-process cactus backend for needle fast path"
```

---

### Task 7: Wire the router into main.py

**Files:**
- Create: `app/needle/factory.py`
- Modify: `app/main.py:50-93` (lifespan + `/api/chat`)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `Settings` needle fields; `MenuProvider`, `FastPathRouter`, `CactusBackend`; `registry.get`, `to_structured_tool`, `LoopGuard`.
- Produces: `build_fast_path_router(cfg, rest, ctx) -> FastPathRouter | None`; `app.state.fast_path` (router or `None`); `/api/chat` consults it before the agent.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py`:

```python
class FakeFastPath:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    async def try_fast_path(self, message, thread_id):
        self.calls.append((message, thread_id))
        return self._reply


class ExplodingAgent:
    async def ainvoke(self, payload, config=None):
        raise AssertionError("agent must not run when fast path handles the turn")


def test_fast_path_handles_turn_and_skips_agent():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = ExplodingAgent()
        client.app.state.fast_path = FakeFastPath("Done — triggered automation.ai_goodnight.")
        resp = client.post("/api/chat", json={"message": "goodnight"})
    assert resp.status_code == 200
    assert resp.json()["reply"].startswith("Done — triggered")


def test_agent_runs_when_fast_path_declines():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeAgent()
        client.app.state.fast_path = FakeFastPath(None)  # declines
        resp = client.post("/api/chat", json={"message": "what's the temperature"})
    assert resp.json() == {"reply": "hi there"}


def test_fast_path_absent_by_default():
    app = create_app(_settings())  # needle_enabled defaults False
    with TestClient(app) as client:
        assert client.app.state.fast_path is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_main.py -q`
Expected: FAIL — `app.state.fast_path` missing / endpoint doesn't consult it.

- [ ] **Step 3: Write the implementation**

`app/needle/factory.py`:

```python
"""Build the FastPathRouter from settings, or None when disabled. Keeps the
cactus import lazy so the runtime is only required when the fast path is on."""

from __future__ import annotations

import logging

from app.needle.menu import MenuProvider
from app.needle.router import FastPathRouter
from app.tools import registry
from app.tools.adapter import LoopGuard, to_structured_tool

log = logging.getLogger("needle")


def build_fast_path_router(cfg, rest, ctx):
    if not cfg.needle_enabled or cfg.max_tier < 2:
        return None
    trigger_defn = registry.get("trigger_automation")
    if trigger_defn is None:
        log.warning("needle enabled but trigger_automation not registered; disabling")
        return None
    trigger_tool = to_structured_tool(trigger_defn, ctx, LoopGuard())
    if cfg.needle_backend == "sidecar":
        from app.needle.sidecar_backend import SidecarBackend
        backend = SidecarBackend(cfg.needle_sidecar_url)
    else:
        from app.needle.cactus_backend import CactusBackend
        backend = CactusBackend(cfg.needle_model_path)
    menu_provider = MenuProvider(rest, ttl_s=cfg.needle_menu_ttl_s)
    return FastPathRouter(backend, menu_provider, trigger_tool,
                          cfg.needle_confidence_threshold)
```

In `app/main.py`, import `from app.needle.factory import build_fast_path_router`. In the lifespan, right after `app.state.agent = build_agent(cfg, ctx)` (registry is loaded by then):

```python
            app.state.fast_path = None
            try:
                app.state.fast_path = build_fast_path_router(cfg, rest, ctx)
            except Exception:
                log.exception("needle fast path failed to build; running agent-only")
```

In the `chat` handler, before the agent call:

```python
    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        router = getattr(app.state, "fast_path", None)
        if router is not None:
            reply = await router.try_fast_path(req.message, req.thread_id)
            if reply is not None:
                return ChatResponse(reply=reply)
        try:
            result = await app.state.agent.ainvoke(...)   # unchanged below
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_main.py -q`
Expected: PASS (all, including the three new ones).

- [ ] **Step 5: Commit**

```bash
git add app/needle/factory.py app/main.py tests/test_main.py
git commit -m "feat: wire needle fast path into /api/chat (gated, disabled by default)"
```

---

### Task 8: Eval harness — cases-needle.yaml + backend selection

**Files:**
- Create: `tests/evals/cases-needle.yaml`
- Modify: `tests/evals/run.py` (add a `--cases` option + a Needle backend path)
- Test: manual eval run (not collected by pytest)

**Interfaces:**
- Consumes: `CactusBackend`, `MenuProvider`, `Decision`; existing `check()` in `run.py`.
- Produces: a runnable comparison of Needle's confident-fraction × accuracy on the trigger subset.

- [ ] **Step 1: Author the case file**

`tests/evals/cases-needle.yaml` — trigger-automation utterances mapped to expected ids. Example entries:

```yaml
- prompt: "goodnight"
  expect_tool: trigger_automation
  expect_params: {entity_id: automation.ai_goodnight}
- prompt: "run the movie scene"
  expect_tool: trigger_automation
  expect_params: {entity_id: automation.ai_movie}
- prompt: "what's the temperature in the kitchen"
  expect_not_tool: trigger_automation   # must fall through, not fire
```

(Populate with the real `automation.ai_*` ids from the test HA instance.)

- [ ] **Step 2: Add a `--cases` flag + Needle path to `run.py`**

Add an `argparse` `--cases` option defaulting to `cases.yaml`; when pointed at `cases-needle.yaml`, run each prompt through a `CactusBackend.classify` against a `MenuProvider`-built menu and adapt the `(entity_id, confidence)` into the `tool_calls` shape `check()` expects (`[{"name": "trigger_automation", "args": {"entity_id": id}}]` when `confidence >= threshold`, else `[]`). Reuse `check()` unchanged. Report confident-fraction and accuracy alongside the existing per-case output.

- [ ] **Step 3: Run the eval against the live model**

Run: `NEEDLE_MODEL_PATH=<path> venv/bin/python -m tests.evals.run --cases cases-needle.yaml`
Expected: prints per-case pass/fail + a summary (confident-fraction, accuracy). This is the go/no-go number — compare against the ~22–23/31 `minicpm-ha` baseline on the trigger subset.

- [ ] **Step 4: Live manual verification**

With `needle_enabled: true`, `max_tier: 2`, the AI-actions switch ON, and a couple of real `automation.ai_*` test automations: start the server (`venv/bin/uvicorn app.main:create_app --factory --port 8099`), POST a trigger phrase to `/api/chat`, and confirm (a) the automation fired, (b) an `agent.actions` audit line was written, (c) latency is ~1–2 s. Then flip the AI-actions switch OFF and confirm the reply is the `ai_disabled` refusal (no fall-through, no trigger).

- [ ] **Step 5: Commit**

```bash
git add tests/evals/cases-needle.yaml tests/evals/run.py
git commit -m "test: needle eval cases + runner backend selection"
```

---

## Self-Review

**Spec coverage:**
- Latency fast path → Tasks 5–7. ✓
- In-process (A) with sidecar (B) fallback → Task 6 + factory branch in Task 7. ✓
- Confident → immediate trigger; unsure → fall through; gate refusal → surfaced not fallen through → Task 5 tests. ✓
- Invoke existing gated tool (safety invariant) → Task 5 (`test_confident_hit_goes_through_real_gated_tool`) + Task 7 factory. ✓
- Menu grammar from `ai_*` set, cached by signature → Tasks 3, 6. ✓
- Ships disabled behind `needle_enabled` + `max_tier >= 2` → Tasks 2, 7 (`test_fast_path_absent_by_default`). ✓
- Fail open on any error → Task 5 (`test_backend_error_falls_through`), Task 7 (build wrapped in try). ✓
- Dedicated `cases-needle.yaml` + eval measurement → Task 8. ✓
- Feasibility spike gates A/B → Task 1. ✓

**Placeholder scan:** Real code in every implementation step. The only deferred content is the three marked `cactus` API lines in Task 6, which are explicitly sourced from the Task 1 spike notes — unavoidable, since the vendor API is unknown until the spike runs.

**Type consistency:** `Decision(entity_id, confidence)`, `Menu(items, signature)`, `MenuItem(entity_id, name)`, `MenuProvider.get()`, `NeedleBackend.classify(message, menu)`, `FastPathRouter.try_fast_path(message, thread_id)`, and `build_fast_path_router(cfg, rest, ctx)` are used identically across Tasks 3–8.
