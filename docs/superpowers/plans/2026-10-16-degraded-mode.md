# Degraded Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Agent Gosling useful when the LLM server is down:
- fail in about 3 s instead of minutes;
- tell the user the LLM is offline;
- keep `/actions`, `/run`, `/notes`, `/cancel` and a fuzzy action matcher working;
- recover automatically when the LLM returns.

**Architecture:**
- A generic `ServiceHealth` tracker (`app/health.py`) watches the LLM and the fast-path service. It combines a background probe with a passive "this call just failed" signal.
- A deterministic `CommandRouter` (`app/degraded/`) sits at the HTTP edge in front of both chat endpoints. It always handles `/commands`, and handles free text only while the LLM is down.
- The router runs the existing tools through `to_structured_tool`, so the AI-actions gate, the audit log and the loop guard all still apply.
- Router replies are English and are never written to the conversation thread.

**Tech Stack:** Python 3.14 (uv), FastAPI, langchain v1 / langgraph, httpx, pytest (asyncio_mode=auto), vanilla JS frontend.

**Spec:** `/Users/ilniko/Desktop/Obsidian/Big Beautiful Brain/Claude Specs/homeassistant-agent/2026-10-16-degraded-mode-design.md` (approved; v3, with the matcher filler-word fix made during planning).

## Global Constraints

- Python: ALWAYS `uv run python` / `uv run pytest`; never system python.
- The test suite stays offline. `uv run pytest -q` must pass with **exactly 1 warning** (the starlette TestClient deprecation); a new warning is a finding.
- Use the langchain v1 API only. Verify API details against the installed venv; don't rely on memory.
- Write plain, readable Python: regular `for` loops rather than comprehensions, and explicit steps.
- Tool output stays `ToolResult…to_json()` envelopes. Handlers never raise into the agent loop.
- Writes are menu-only: the router may call only `trigger_action` (via the adapter, which enforces the AI switch gate) and `cancel_scheduled`.
- Use a regular feature branch (`feat/degraded-mode`); no worktrees. Never commit to `main`; open a PR at the end.
- Probes use a 3 s timeout and run every 15 s while `down`/`unknown` and every 60 s while `up`.
- Both LLM clients use `httpx.Timeout(300.0, connect=3.0)`; ChatOpenAI also uses `max_retries=1`.
- Matcher: fire when `best ≥ 0.80` **and** `best − second ≥ 0.15`; suggest candidates scoring `≥ 0.50`, at most 3.
- Router replies are **English**. Slash commands, and any text while the LLM is down, skip `language.inbound()` and `outbound()`.
- Router replies are **ephemeral**: never written to the checkpointed thread.
- `/api/health` **always returns HTTP 200**.

## Review Focus

1. **Free text with a scheduling cue while the LLM is down** ("when the vacuum starts, stop it"). It must not trigger the action immediately; the user gets the "can't schedule while offline" reply. *Pinned in Task 9 (`test_fallback_refuses_deferred_request`).*
2. **An ambiguous action name** ("living room lights" when both On and Off exist, scoring 0.819 and 0.81). It must never fire; the user gets a numbered "did you mean" list that `/run <n>` accepts. *Pinned in Task 7 (`test_near_tie_gives_candidates`) and Task 8 (`test_run_ambiguous_name_lists_candidates_with_actions_numbers`).*
3. **The AI-actions switch is off while degraded.** `/run` and the matcher must refuse through the existing gate, with the `ai_disabled` message. *Pinned in Task 8 (`test_run_refused_when_ai_switch_off`).*
4. **The LLM fails mid-request while the state is still `unknown`.** The user still gets a reply for that same message instead of a 500 or a cut stream, and the thread doesn't keep a dangling user message. *Pinned in Task 10 (`test_chat_outage_returns_degraded_reply_and_marks_down`, `test_stream_outage_ends_with_degraded_done`, `test_close_dangling_turn_appends_marker`).*
5. **The router itself crashes** (a bug, or an odd HA reply). That must never become a 500; the user gets the generic offline reply. *Pinned in Task 10 (`test_router_crash_returns_offline_reply`).*

---

## File map

| File | Status | Responsibility |
|---|---|---|
| `app/health.py` | create | `ServiceHealth`: the state, the probe loop, the passive `mark_down`, recovery callbacks |
| `app/agent/llm_health.py` | create | `is_llm_outage`, `build_llm_probe`, `build_fast_path_probe`, `needs_reresolve`, `reresolve` |
| `app/agent/llm.py` | modify | fail-fast timeouts and retries |
| `app/agent/history.py` | modify | `UNANSWERED_MARKER`, `close_dangling_turn`; `display_transcript` hides the marker |
| `app/agent/streaming.py` | modify | yield `llm_unavailable` on an outage instead of raising |
| `app/agent/middleware/fast_path_middleware.py` | modify | skip classification while the fast path is down; passive `mark_down` |
| `app/agent/middleware/__init__.py`, `app/agent/factory.py` | modify | thread `fast_path_health` through |
| `app/degraded/__init__.py` | create | the `Reply` dataclass |
| `app/degraded/tools.py` | create | `ToolRunner`: runs registry tools through the adapter and records calls |
| `app/degraded/formatters.py` | create | pure envelope → English text functions |
| `app/degraded/matcher.py` | create | pure fuzzy matching of text against action names |
| `app/degraded/commands.py` | create | `/help /actions /run /notes /cancel`, plus the shared action helpers |
| `app/degraded/router.py` | create | `CommandRouter.handle`: slash dispatch and the natural-language fallback chain |
| `app/main.py` | modify | lifespan wiring, `/api/health`, router branches in both chat endpoints |
| `frontend/index.html`, `frontend/app.js`, `frontend/style.css` | modify | banner, degraded bubble, command chips |
| `README.md`, `CLAUDE.md` | modify | docs |

---

### Task 0: Branch

- [ ] **Step 1: Create the feature branch**

```bash
git checkout main && git pull && git checkout -b feat/degraded-mode
```

- [ ] **Step 2: Baseline**

Run: `uv run pytest -q`
Expected: `652 passed, 1 warning` (or the current count with exactly 1 warning). Note the number.

---

### Task 1: Fail-fast LLM clients

**Files:**
- Modify: `app/agent/llm.py:12-20,39,58-73`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `LLM_HTTP_TIMEOUT: httpx.Timeout`, `LLM_MAX_RETRIES: int` in `app.agent.llm`. Nothing else depends on them.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_llm.py`)

```python
def test_ollama_client_fails_fast_on_connect():
    from app.agent.llm import build_llm
    from app.config import Settings

    llm = build_llm(Settings(_env_file=None, llm_provider="ollama"))
    timeout = llm.client_kwargs["timeout"]
    assert timeout.connect == 3.0
    assert timeout.read == 300.0


def test_openai_compatible_clients_fail_fast_and_retry_once():
    from app.agent.llm import build_llm
    from app.config import Settings

    for provider in ("llamacpp", "openai"):
        llm = build_llm(Settings(_env_file=None, llm_provider=provider, llm_url="http://x/v1"))
        assert llm.request_timeout.connect == 3.0
        assert llm.request_timeout.read == 300.0
        assert llm.max_retries == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_llm.py -k fail_fast -v`
Expected: FAIL. `client_kwargs["timeout"]` is the int `300` (no `.connect`), and `request_timeout` is `None`.

- [ ] **Step 3: Implement.** In `app/agent/llm.py`, add `import httpx` and replace the `OLLAMA_HTTP_TIMEOUT` block:

```python
# Generations on modest hardware can exceed a minute, so reads get 300 s. A
# dead or powered-off host must fail in seconds, so connecting gets 3 s —
# never use httpx defaults, which would wait out the OS connect timeout.
LLM_HTTP_TIMEOUT = httpx.Timeout(300.0, connect=3.0)
# One retry rides out a blip; the SDK default (2, with backoff) multiplies an
# outage into minutes.
LLM_MAX_RETRIES = 1
```

In `_build_ollama`: `client_kwargs={"timeout": LLM_HTTP_TIMEOUT},`.
In `_openai_compat_kwargs`, add to the returned dict:

```python
        "timeout": LLM_HTTP_TIMEOUT,
        "max_retries": LLM_MAX_RETRIES,
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_llm.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add app/agent/llm.py tests/test_llm.py
git commit -m "feat(llm): fail fast on unreachable LLM (3 s connect, 1 retry)"
```

---

### Task 2: `ServiceHealth` tracker

**Files:**
- Create: `app/health.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Produces, in `app.health`:
  - constants `UNKNOWN`, `UP`, `DOWN` (str);
  - `ServiceHealth(name: str, probe: Callable[[], Awaitable[bool]], down_interval_s: float = 15.0, up_interval_s: float = 60.0, initial: str = UNKNOWN)`;
  - attributes `.state: str`, `.since: float` (epoch), `.last_error: str | None`;
  - property `.is_down -> bool`;
  - `on_recovery(cb: Callable[[], Awaitable[None]])`;
  - `mark_down(error: object = None) -> None` (sync);
  - `async mark_up() -> None`;
  - `async check() -> None`;
  - `interval() -> float`;
  - `start() -> None`;
  - `async stop() -> None`;
  - `since_hhmm() -> str`;
  - `snapshot() -> dict` with keys `state`, `since`, `error`.

- [ ] **Step 1: Write the failing tests** — `tests/test_health.py`

```python
import asyncio

from app.health import DOWN, UNKNOWN, UP, ServiceHealth


def _probe(results):
    calls = []

    async def probe():
        calls.append(1)
        value = results.pop(0) if results else True
        if isinstance(value, Exception):
            raise value
        return value

    return probe, calls


async def test_first_ok_probe_is_up_without_recovery_callback():
    probe, _ = _probe([True])
    health = ServiceHealth("llm", probe)
    fired = []

    async def callback():
        fired.append(1)

    health.on_recovery(callback)
    assert health.state == UNKNOWN
    await health.check()
    assert health.state == UP
    assert fired == []


async def test_failed_probe_marks_down():
    probe, _ = _probe([False])
    health = ServiceHealth("llm", probe)
    await health.check()
    assert health.state == DOWN
    assert health.is_down
    assert health.last_error == "probe failed"


async def test_probe_exception_marks_down_with_its_message():
    probe, _ = _probe([ConnectionError("refused")])
    health = ServiceHealth("llm", probe)
    await health.check()
    assert health.state == DOWN
    assert "refused" in health.last_error


async def test_recovery_callback_fires_once_on_down_to_up():
    probe, _ = _probe([False, True, True])
    health = ServiceHealth("llm", probe)
    fired = []

    async def callback():
        fired.append(1)

    health.on_recovery(callback)
    await health.check()
    await health.check()
    await health.check()
    assert fired == [1]
    assert health.last_error is None


async def test_failing_callback_does_not_break_recovery():
    probe, _ = _probe([])
    health = ServiceHealth("llm", probe)

    async def callback():
        raise RuntimeError("boom")

    health.on_recovery(callback)
    health.mark_down("x")
    await health.mark_up()
    assert health.state == UP


def test_mark_down_keeps_original_since_but_updates_error():
    probe, _ = _probe([])
    health = ServiceHealth("llm", probe, initial=UP)
    health.mark_down("first")
    since = health.since
    health.mark_down("second")
    assert health.since == since
    assert health.last_error == "second"


def test_interval_depends_on_state():
    probe, _ = _probe([])
    health = ServiceHealth("llm", probe, down_interval_s=15, up_interval_s=60)
    assert health.interval() == 15
    health.state = UP
    assert health.interval() == 60
    health.mark_down()
    assert health.interval() == 15


async def test_start_probes_in_background_and_stop_cancels():
    probe, calls = _probe([True])
    health = ServiceHealth("llm", probe, up_interval_s=3600)
    health.start()
    await asyncio.sleep(0.01)
    assert calls == [1]
    assert health.state == UP
    await health.stop()
    assert health._task is None


def test_snapshot_shape():
    probe, _ = _probe([])
    health = ServiceHealth("llm", probe, initial=DOWN)
    health.mark_down("boom")
    snap = health.snapshot()
    assert snap["state"] == "down"
    assert snap["error"] == "boom"
    assert "T" in snap["since"]
    assert len(health.since_hhmm()) == 5
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_health.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.health'`.

- [ ] **Step 3: Implement** — `app/health.py`

```python
"""Liveness of one HTTP dependency (the LLM server, the fast-path service).

State is "unknown" until the first probe, then "up" or "down". A background
loop probes every down_interval_s while not up (so recovery is noticed fast)
and every up_interval_s while up. Callers also report failures they hit
themselves with mark_down() — the passive signal — so requests stop waiting on
a dead service before the next probe runs. Nothing here raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

log = logging.getLogger("agent.health")

UNKNOWN = "unknown"
UP = "up"
DOWN = "down"

Probe = Callable[[], Awaitable[bool]]
RecoveryCallback = Callable[[], Awaitable[None]]


class ServiceHealth:
    def __init__(
        self,
        name: str,
        probe: Probe,
        down_interval_s: float = 15.0,
        up_interval_s: float = 60.0,
        initial: str = UNKNOWN,
    ) -> None:
        self.name = name
        self._probe = probe
        self._down_interval_s = down_interval_s
        self._up_interval_s = up_interval_s
        self.state = initial
        self.since = time.time()
        self.last_error: str | None = None
        self._callbacks: list[RecoveryCallback] = []
        self._task: asyncio.Task | None = None

    @property
    def is_down(self) -> bool:
        return self.state == DOWN

    def on_recovery(self, callback: RecoveryCallback) -> None:
        self._callbacks.append(callback)

    def mark_down(self, error: object = None) -> None:
        if error is not None:
            self.last_error = str(error) or type(error).__name__
        if self.state == DOWN:
            return
        self.state = DOWN
        self.since = time.time()
        log.warning("%s: down (%s)", self.name, self.last_error)

    async def mark_up(self) -> None:
        previous = self.state
        if previous == UP:
            return
        downtime_s = round(time.time() - self.since)
        self.state = UP
        self.since = time.time()
        self.last_error = None
        if previous != DOWN:
            return  # unknown -> up is the first probe, not a recovery
        log.info("%s: up again after %ss", self.name, downtime_s)
        for callback in self._callbacks:
            try:
                await callback()
            except Exception:
                log.exception("%s: recovery callback failed", self.name)

    async def check(self) -> None:
        error: object = None
        try:
            ok = await self._probe()
            if not ok:
                error = "probe failed"
        except Exception as exc:
            ok = False
            error = exc
        if ok:
            await self.mark_up()
        else:
            self.mark_down(error)

    def interval(self) -> float:
        if self.state == UP:
            return self._up_interval_s
        return self._down_interval_s

    async def _loop(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(self.interval())

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def since_hhmm(self) -> str:
        return datetime.fromtimestamp(self.since).strftime("%H:%M")

    def snapshot(self) -> dict:
        since = datetime.fromtimestamp(self.since).astimezone()
        return {
            "state": self.state,
            "since": since.isoformat(timespec="seconds"),
            "error": self.last_error,
        }
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_health.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add app/health.py tests/test_health.py
git commit -m "feat(health): ServiceHealth tracker with probe loop and passive signal"
```

---

### Task 3: LLM and fast-path probes, outage classification, re-resolve

**Files:**
- Create: `app/agent/llm_health.py`
- Test: `tests/test_llm_health.py`

**Interfaces:**
- Consumes: `resolve_llm_settings(cfg, client=None) -> Settings` from `app.agent.llm_probe`.
- Produces, in `app.agent.llm_health`:
  - `is_llm_outage(exc: BaseException) -> bool`
  - `build_llm_probe(settings: Settings, client: httpx.AsyncClient, unresolved: bool = False) -> Callable[[], Awaitable[bool]]`
  - `build_fast_path_probe(url: str, client: httpx.AsyncClient) -> Callable[[], Awaitable[bool]]`
  - `is_fast_path_outage(exc: BaseException) -> bool`
  - `needs_reresolve(cfg: Settings, run_cfg: Settings) -> bool`
  - `async reresolve(cfg: Settings, current: Settings) -> Settings | None`

- [ ] **Step 1: Write the failing tests** — `tests/test_llm_health.py`

```python
import httpx
import ollama
import openai
from langgraph.errors import GraphRecursionError

from app.agent import llm_health
from app.agent.llm_health import (
    build_fast_path_probe,
    build_llm_probe,
    is_fast_path_outage,
    is_llm_outage,
    needs_reresolve,
    reresolve,
)
from app.config import Settings


def _client(routes):
    """routes: path -> status code, or an Exception to raise."""
    def handler(request):
        outcome = routes.get(request.url.path)
        if outcome is None:
            return httpx.Response(404)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _settings(**values):
    return Settings(_env_file=None, **values)


async def test_llamacpp_probe_uses_health_without_v1():
    probe = build_llm_probe(
        _settings(llm_provider="llamacpp", llm_url="http://llm:8080/v1"),
        _client({"/health": 200}),
    )
    assert await probe() is True


async def test_llamacpp_loading_model_counts_as_down():
    probe = build_llm_probe(
        _settings(llm_provider="llamacpp", llm_url="http://llm:8080"),
        _client({"/health": 503}),
    )
    assert await probe() is False


async def test_ollama_probe_uses_api_version():
    probe = build_llm_probe(
        _settings(llm_provider="ollama", llm_url="http://llm:11434"),
        _client({"/api/version": 200}),
    )
    assert await probe() is True


async def test_openai_compatible_probe_uses_models():
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="https://api.example.com/v1"),
        _client({"/v1/models": 200}),
    )
    assert await probe() is True


async def test_unresolved_probe_accepts_any_known_endpoint():
    probe = build_llm_probe(
        _settings(llm_provider="openai", llm_url="http://llm:11434"),
        _client({"/api/tags": 200}),
        unresolved=True,
    )
    assert await probe() is True


async def test_probe_connection_error_is_down():
    probe = build_llm_probe(
        _settings(llm_provider="ollama", llm_url="http://llm:11434"),
        _client({"/api/version": httpx.ConnectError("refused")}),
    )
    assert await probe() is False


async def test_fast_path_probe():
    up = build_fast_path_probe("http://needle:8000/", _client({"/health": 200}))
    down = build_fast_path_probe("http://needle:8000", _client({"/health": httpx.ConnectError("x")}))
    assert await up() is True
    assert await down() is False


def test_is_llm_outage_true_cases():
    request = httpx.Request("POST", "http://llm/v1/chat/completions")
    cases = [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        ConnectionError("Failed to connect to Ollama"),
        openai.APIConnectionError(request=request),
        openai.APITimeoutError(request=request),
        openai.InternalServerError("boom", response=httpx.Response(500, request=request), body=None),
        ollama.ResponseError("overloaded", 503),
    ]
    for exc in cases:
        assert is_llm_outage(exc), exc


def test_is_llm_outage_false_cases():
    for exc in (ValueError("bad output"), ollama.ResponseError("bad request", 400),
                GraphRecursionError("limit")):
        assert not is_llm_outage(exc), exc


def test_is_llm_outage_follows_exception_chain():
    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_llm_outage(outer)


def test_is_fast_path_outage():
    assert is_fast_path_outage(httpx.ConnectError("x"))
    assert not is_fast_path_outage(ValueError("bad json"))


def test_needs_reresolve():
    auto = _settings()
    assert needs_reresolve(auto, auto.model_copy(update={"llm_provider": "openai"})) is True
    assert needs_reresolve(auto, auto.model_copy(update={"llm_provider": "ollama"})) is False
    keyed = _settings(api_key="k")
    assert needs_reresolve(keyed, keyed.model_copy(update={"llm_provider": "openai"})) is False
    explicit = _settings(llm_provider="llamacpp")
    assert needs_reresolve(explicit, explicit) is False


async def test_reresolve_returns_new_settings_only_when_changed(monkeypatch):
    cfg = _settings()
    current = cfg.model_copy(update={"llm_provider": "openai"})

    async def to_ollama(c, client=None):
        return c.model_copy(update={"llm_provider": "ollama"})

    monkeypatch.setattr(llm_health, "resolve_llm_settings", to_ollama)
    fresh = await reresolve(cfg, current)
    assert fresh is not None
    assert fresh.llm_provider == "ollama"

    async def still_openai(c, client=None):
        return c.model_copy(update={"llm_provider": "openai"})

    monkeypatch.setattr(llm_health, "resolve_llm_settings", still_openai)
    assert await reresolve(cfg, current) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_llm_health.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.agent.llm_health'`.

- [ ] **Step 3: Implement** — `app/agent/llm_health.py`

```python
"""LLM- and fast-path-specific health pieces: the probes ServiceHealth runs,
the rule for which exceptions mean "the service is down" (as opposed to "the
model answered badly"), and re-resolving an `auto` provider that could not be
identified at boot."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import ollama
import openai

from app.agent.llm_probe import resolve_llm_settings
from app.config import Settings

_PROBE_TIMEOUT = 3.0

Probe = Callable[[], Awaitable[bool]]


def _is_outage(exc: BaseException) -> bool:
    # httpx.TransportError covers connect/read timeouts, refused connections
    # and dropped streams. Ollama's client re-raises httpx.ConnectError as a
    # builtin ConnectionError "from None", so the chain is lost — match it too.
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return True
    # APITimeoutError is a subclass of APIConnectionError.
    if isinstance(exc, (openai.APIConnectionError, openai.InternalServerError)):
        return True
    if isinstance(exc, ollama.ResponseError):
        return getattr(exc, "status_code", -1) >= 500
    return False


def is_llm_outage(exc: BaseException) -> bool:
    """True when *exc* (or anything in its cause/context chain) means the LLM
    server could not be reached or failed server-side. Bad model output,
    validation errors and recursion limits are not outages."""
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _is_outage(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_fast_path_outage(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError)


def _strip_v1(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url[: -len("/v1")]
    return url


def _probe_urls(settings: Settings, unresolved: bool) -> list[str]:
    base = settings.llm_url.rstrip("/")
    root = _strip_v1(base)
    if unresolved:
        # Same endpoints the boot-time probe tried, plus the generic one.
        return [root + "/props", root + "/api/tags", base + "/models"]
    if settings.llm_provider == "llamacpp":
        return [root + "/health"]  # 503 while the model is loading
    if settings.llm_provider == "ollama":
        return [base + "/api/version"]
    return [base + "/models"]


async def _any_ok(client: httpx.AsyncClient, urls: list[str]) -> bool:
    for url in urls:
        try:
            response = await client.get(url, timeout=_PROBE_TIMEOUT)
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            return True
    return False


def build_llm_probe(settings: Settings, client: httpx.AsyncClient, unresolved: bool = False) -> Probe:
    urls = _probe_urls(settings, unresolved)

    async def probe() -> bool:
        return await _any_ok(client, urls)

    return probe


def build_fast_path_probe(url: str, client: httpx.AsyncClient) -> Probe:
    urls = [url.rstrip("/") + "/health"]

    async def probe() -> bool:
        return await _any_ok(client, urls)

    return probe


def needs_reresolve(cfg: Settings, run_cfg: Settings) -> bool:
    """True when the boot-time `auto` probe fell back to the OpenAI-compatible
    client: either the server was down, or it really is a plain OpenAI-style
    server. Re-resolving on recovery is harmless in the second case."""
    return cfg.llm_provider == "auto" and not cfg.api_key and run_cfg.llm_provider == "openai"


async def reresolve(cfg: Settings, current: Settings) -> Settings | None:
    """Probe the backend again. Returns the new settings when the provider or
    the context size changed (the agent must be rebuilt), else None."""
    fresh = await resolve_llm_settings(cfg)
    if fresh.llm_provider == current.llm_provider and fresh.num_ctx == current.num_ctx:
        return None
    return fresh
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_llm_health.py -v`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
git add app/agent/llm_health.py tests/test_llm_health.py
git commit -m "feat(health): LLM/fast-path probes, outage classification, re-resolve"
```

---

### Task 4: Fast path skips while down and reports failures

**Files:**
- Modify: `app/agent/middleware/fast_path_middleware.py` (`__init__`, `awrap_model_call`, `_classify_with_telemetry`)
- Modify: `app/agent/middleware/__init__.py` (`build_middleware`)
- Modify: `app/agent/factory.py` (`build_agent`)
- Test: `tests/test_fast_path_middleware.py`

**Interfaces:**
- Consumes: `ServiceHealth` (Task 2); `is_fast_path_outage` (Task 3).
- Produces:
  - `FastPathMiddleware(backend, menu_provider, threshold, tracer=None, store_conn=None, health=None)`;
  - `build_middleware(..., fast_path_health=None)`;
  - `build_agent(settings, ctx, checkpointer=None, fast_path=None, telemetry=None, enabled=None, fast_path_health=None)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_fast_path_middleware.py`)

```python
@pytest.mark.asyncio
async def test_skips_classification_while_fast_path_down():
    from app.health import ServiceHealth

    class _Counting:
        name = "counting"

        def __init__(self):
            self.calls = 0

        async def classify(self, message, menu):
            # Counted, not raised: the middleware swallows classify errors,
            # so raising here would pass even if the skip were missing.
            self.calls += 1
            return Decision("automation.ai_action_night", 0.9)

    async def _probe():
        return False

    backend = _Counting()
    health = ServiceHealth("fast_path", _probe)
    health.mark_down("off")
    mw = FastPathMiddleware(backend, _FakeMenu(_MENU), threshold=0.0, health=health)

    async def handler(request):
        return AIMessage("llm reply")

    resp = await mw.awrap_model_call(_request([HumanMessage("goodnight")]), handler)
    assert resp.content == "llm reply"
    assert backend.calls == 0


@pytest.mark.asyncio
async def test_connection_error_marks_fast_path_down():
    import httpx
    from app.health import DOWN, ServiceHealth

    class _Refused:
        name = "refused"
        async def classify(self, message, menu):
            raise httpx.ConnectError("refused")

    async def _probe():
        return True

    health = ServiceHealth("fast_path", _probe)
    mw = FastPathMiddleware(_Refused(), _FakeMenu(_MENU), threshold=0.0, health=health)

    async def handler(request):
        return AIMessage("llm reply")

    resp = await mw.awrap_model_call(_request([HumanMessage("goodnight")]), handler)
    assert resp.content == "llm reply"
    assert health.state == DOWN
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_fast_path_middleware.py -k "fast_path_down" -v`
Expected: FAIL with `TypeError: FastPathMiddleware.__init__() got an unexpected keyword argument 'health'`.

- [ ] **Step 3: Implement**

In `fast_path_middleware.py`, add the import `from app.agent.llm_health import is_fast_path_outage`.

Extend `__init__`:

```python
    def __init__(self, backend, menu_provider, threshold: float, tracer=None, store_conn=None,
                 health=None):
        super().__init__()
        self._backend = backend
        self._menu_provider = menu_provider
        self._threshold = threshold
        self._tracer = tracer
        self._store_conn = store_conn
        self._health = health  # ServiceHealth for the fast-path service, or None
```

Add a helper method:

```python
    def _note_failure(self, exc: Exception) -> None:
        """Passive health signal: a refused/timed-out classify marks the
        service down so the next requests skip it without waiting."""
        if self._health is not None and is_fast_path_outage(exc):
            self._health.mark_down(exc)
```

In `awrap_model_call`, in the `if isinstance(last, HumanMessage):` branch, right after the `_opted_out` block, add:

```python
            if self._health is not None and self._health.is_down:
                self._record_skip("fast_path_down")
                return await handler(request)
```

In `_classify_with_telemetry`, change **each** of the three `except Exception:` blocks that wrap `self._backend.classify(...)` to `except Exception as exc:` and call `self._note_failure(exc)` as their first line. These are the plain path, the span-creation fallback, and the traced `decision = await self._backend.classify(...)`. Leave the menu-fetch and last-resort blocks unchanged.

In `app/agent/middleware/__init__.py`, give `build_middleware` a `fast_path_health=None` parameter and pass `health=fast_path_health` into `FastPathMiddleware(...)`.

In `app/agent/factory.py`, give `build_agent` a `fast_path_health=None` parameter (document it in the docstring's Args: "ServiceHealth of the fast-path service; the middleware skips classification while it is down") and pass `fast_path_health=fast_path_health` to `build_middleware(...)`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_fast_path_middleware.py tests/test_factory.py tests/test_fast_path_wiring.py -v`
Expected: PASS (all, including the existing ones).

- [ ] **Step 5: Commit**

```bash
git add app/agent/middleware/fast_path_middleware.py app/agent/middleware/__init__.py app/agent/factory.py tests/test_fast_path_middleware.py
git commit -m "feat(fast-path): skip classification while the service is down"
```

---

### Task 5: Lifespan wiring, `/api/health`, provider hot-swap on recovery

**Files:**
- Modify: `app/main.py` (the lifespan in `create_app`, `/api/health`)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `ServiceHealth`, `DOWN`, `UNKNOWN` (Task 2); `build_llm_probe`, `build_fast_path_probe`, `needs_reresolve`, `reresolve` (Task 3); `build_agent(..., fast_path_health=)` (Task 4).
- Produces:
  - `app.state.llm_health: ServiceHealth`;
  - `app.state.fp_health: ServiceHealth | None`;
  - `/api/health` JSON keys `status, ha, llm, llm_state, llm_since, llm_error, provider, fast_path_state, websocket`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_main.py`)

```python
def test_health_reports_tracker_state_and_stays_200():
    app = create_app(_settings())
    with TestClient(app) as client:
        resp = client.get("/api/health")
    body = resp.json()
    assert resp.status_code == 200
    for key in ("llm_state", "llm_since", "llm_error", "provider", "fast_path_state"):
        assert key in body
    assert body["llm_state"] in ("unknown", "down")
    assert body["llm"] is False
    assert body["fast_path_state"] is None


def test_health_llm_true_when_tracker_up():
    from app.health import UP

    app = create_app(_settings())
    with TestClient(app) as client:
        client.portal.call(client.app.state.llm_health.stop)
        client.app.state.llm_health.state = UP
        body = client.get("/api/health").json()
    assert body["llm"] is True
    assert body["llm_state"] == "up"


def test_unresolved_auto_provider_hot_swaps_agent_on_recovery(monkeypatch):
    from app.agent import llm_health as llm_health_mod

    async def boot_probe_failed(cfg, client=None):
        return cfg.model_copy(update={"llm_provider": "openai"})

    async def now_ollama(cfg, client=None):
        return cfg.model_copy(update={"llm_provider": "ollama"})

    monkeypatch.setattr("app.main.resolve_llm_settings", boot_probe_failed)
    monkeypatch.setattr(llm_health_mod, "resolve_llm_settings", now_ollama)
    app = create_app(_settings())
    with TestClient(app) as client:
        state = client.app.state
        client.portal.call(state.llm_health.stop)
        assert state.llm_health.state == "down"
        old_agent = state.agent
        client.portal.call(state.llm_health.mark_up)
        assert state.settings.llm_provider == "ollama"
        assert state.agent is not old_agent
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_main.py -k "health or hot_swap" -v`
Expected: FAIL: `KeyError`/`AssertionError` on `llm_state`, and `AttributeError: 'State' object has no attribute 'llm_health'`.

- [ ] **Step 3: Implement** in `app/main.py`.

Add imports:

```python
from app.agent.llm_health import (
    build_fast_path_probe,
    build_llm_probe,
    needs_reresolve,
    reresolve,
)
from app.health import DOWN, UNKNOWN, ServiceHealth
```

In the lifespan, right after `run_cfg = await resolve_llm_settings(cfg)`:

```python
            # One client for every health probe; closed in `finally`.
            health_client = httpx.AsyncClient()
            unresolved = needs_reresolve(cfg, run_cfg)
            initial = DOWN if unresolved else UNKNOWN
            llm_health = ServiceHealth(
                "llm", build_llm_probe(run_cfg, health_client, unresolved), initial=initial,
            )
            app.state.llm_health = llm_health
```

Then, right after the `fast_path = build_fast_path_backend(...)` try/except:

```python
            fp_health = None
            if fast_path is not None:
                fp_health = ServiceHealth(
                    "fast_path", build_fast_path_probe(run_cfg.fast_path_url, health_client),
                )
            app.state.fp_health = fp_health
```

Pass `fast_path_health=fp_health` to the existing `build_agent(...)` call. Inside `async with open_checkpointer(...)`, after `app.state.agent = agent`, add:

```python
                async def _on_llm_recovered() -> None:
                    # Only an `auto` provider that could not be identified at
                    # boot needs this; an explicit provider never changes.
                    if not unresolved:
                        return
                    fresh = await reresolve(cfg, app.state.settings)
                    if fresh is None:
                        return
                    log.info("llm: provider resolved to %s on recovery; rebuilding agent",
                             fresh.llm_provider)
                    app.state.agent = build_agent(
                        fresh, ctx, checkpointer=checkpointer, fast_path=fast_path,
                        telemetry=telemetry_arg, enabled=enabled, fast_path_health=fp_health,
                    )
                    app.state.settings = fresh

                llm_health.on_recovery(_on_llm_recovered)
                llm_health.start()
                if fp_health is not None:
                    fp_health.start()
```

`health_client`, `llm_health` and `fp_health` are created inside the outer `try:`, but the `finally:` must still close them. So initialise all three to `None` just before that `try:` (next to `snapshot_conn`). Then add to the top of `finally:`:

```python
            if llm_health is not None:
                await llm_health.stop()
            if fp_health is not None:
                await fp_health.stop()
            if health_client is not None:
                await health_client.aclose()
```

Replace the whole `/api/health` handler:

```python
    @app.get("/api/health")
    async def health() -> dict:
        # Always HTTP 200: the Supervisor watchdog polls this endpoint, and a
        # non-200 during an LLM outage would restart-loop the add-on. The body
        # carries the real state; the trackers are read, never probed here.
        ha_ok = False
        try:
            ha_ok = await app.state.rest.ping()
        except Exception:
            pass
        llm_health = getattr(app.state, "llm_health", None)
        if llm_health is not None:
            llm_snap = llm_health.snapshot()
        else:
            llm_snap = {"state": UNKNOWN, "since": None, "error": None}
        llm_ok = llm_snap["state"] == "up"
        fp_health = getattr(app.state, "fp_health", None)
        fast_path_state = fp_health.state if fp_health is not None else None
        ws_ok = app.state.ws is not None and app.state.ws.connected
        return {
            "status": "ok" if (ha_ok and llm_ok) else "degraded",
            "ha": ha_ok,
            "llm": llm_ok,
            "llm_state": llm_snap["state"],
            "llm_since": llm_snap["since"],
            "llm_error": llm_snap["error"],
            "provider": app.state.settings.llm_provider,
            "fast_path_state": fast_path_state,
            "websocket": ws_ok,
        }
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_main.py -v` and then `uv run pytest -q`
Expected: PASS, with exactly 1 warning.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat(health): wire LLM/fast-path trackers, /api/health from tracker, hot-swap on recovery"
```

---

### Task 6: Degraded package base: `Reply`, `ToolRunner`, formatters

**Files:**
- Create: `app/degraded/__init__.py`, `app/degraded/tools.py`, `app/degraded/formatters.py`
- Test: `tests/test_degraded_formatters.py`, `tests/test_degraded_tools.py`

**Interfaces:**
- Consumes: `registry.get`, `to_structured_tool`, `LoopGuard`.
- Produces:
  - `Reply(text: str, kind: str = "command", tool_calls: list[dict] = [])`, where `kind` is `"command"` or `"degraded"`, and each tool call is `{"name": str, "status": str}`;
  - `ToolRunner(ctx, enabled: set[str] | None, thread_id: str)` with `.has(name) -> bool`, `async .run(name, args) -> dict` (the envelope dict) and `.calls: list[dict]`;
  - formatters `display_action_name(name, entity_id="") -> str`, `sorted_action_rows(rows) -> list[dict]`, `format_actions(rows, total) -> str`, `format_did_you_mean(candidates, rows) -> str`, `format_trigger(envelope, name, params) -> str`, `format_notes(envelope) -> str`, `format_error(envelope) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_degraded_formatters.py`:

```python
from app.degraded.formatters import (
    display_action_name,
    format_actions,
    format_did_you_mean,
    format_error,
    format_notes,
    format_trigger,
    sorted_action_rows,
)

ROWS = [
    {"entity_id": "script.ai_action_stop_vacuum", "name": "AI Action: Stop Vacuum"},
    {"entity_id": "automation.ai_goodnight", "name": "Goodnight"},
    {"entity_id": "script.ai_action_lights_on", "name": "AI Action: Living Room Lights On"},
]


def test_display_action_name_strips_prefix_and_falls_back_to_entity_id():
    assert display_action_name("AI Action: Stop Vacuum") == "Stop Vacuum"
    assert display_action_name("", "script.ai_action_stop_vacuum") == "action stop vacuum"


def test_sorted_action_rows_by_display_name():
    names = []
    for row in sorted_action_rows(ROWS):
        names.append(display_action_name(row["name"], row["entity_id"]))
    assert names == ["Goodnight", "Living Room Lights On", "Stop Vacuum"]


def test_format_actions_numbers_rows():
    text = format_actions(sorted_action_rows(ROWS), total=3)
    assert "1. Goodnight" in text
    assert "3. Stop Vacuum" in text
    assert "/run" in text


def test_format_actions_empty_and_truncated():
    assert format_actions([], total=0) == "No AI actions are available."
    assert "showing 1 of 5" in format_actions(sorted_action_rows(ROWS[:1]), total=5)


def test_format_did_you_mean_uses_actions_numbering():
    rows = sorted_action_rows(ROWS)
    text = format_did_you_mean([rows[2], rows[1]], rows)
    assert "3. Stop Vacuum" in text
    assert "2. Living Room Lights On" in text
    assert "/run <number>" in text


def test_format_trigger_ok_and_error():
    ok = {"status": "ok", "data": {"triggered": True}}
    assert format_trigger(ok, "Lights On", {}) == "Done — triggered Lights On."
    assert format_trigger(ok, "Dim", {"level": 30}) == "Done — triggered Dim (level: 30)."
    refused = {"status": "error", "error": {"code": "ai_disabled", "message": "AI-triggered actions are turned off at the home level."}}
    assert format_trigger(refused, "Lights On", {}).startswith("AI-triggered actions are turned off")


def test_format_error_maps_unreachable():
    env = {"status": "error", "error": {"code": "ha_unreachable", "message": "Could not reach Home Assistant: boom."}}
    assert format_error(env) == "Couldn't reach Home Assistant."


def test_format_notes():
    env = {"status": "ok", "data": {"rows": [
        {"id": 12, "kind": "action", "instruction": "Stop the vacuum.", "when": "18:00 16-10-2026"},
    ], "total": 1}}
    assert format_notes(env) == "Scheduled tasks:\n#12 — 18:00 16-10-2026: Stop the vacuum.\nCancel one with /cancel <id>."
    assert format_notes({"status": "ok", "data": {"rows": [], "total": 0}}) == "No scheduled tasks."
```

`tests/test_degraded_tools.py`:

```python
import pytest

from app.config import Settings
from app.degraded.tools import ToolRunner
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    def __init__(self, switch="on"):
        self.switch = switch
        self.calls = []

    async def get_state(self, entity_id):
        if entity_id == "input_boolean.ai_triggered_actions":
            return {"entity_id": entity_id, "state": self.switch, "attributes": {}}
        return {"entity_id": entity_id, "state": "off", "attributes": {}}

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id))
        return []


@pytest.fixture(autouse=True)
def tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.action.trigger_action",))
    yield
    registry._reset_for_tests()


def _runner(rest, enabled=None, allow_actions=True):
    settings = Settings(_env_file=None, allow_actions=allow_actions)
    ctx = ToolContext(settings=settings, rest=rest, ws=None)
    return ToolRunner(ctx, enabled, "t1")


async def test_run_goes_through_adapter_and_records_call():
    rest = FakeRest()
    runner = _runner(rest)
    env = await runner.run("trigger_action", {"entity_id": "automation.ai_goodnight", "params": {}})
    assert env["status"] == "ok"
    assert rest.calls == [("automation", "trigger", "automation.ai_goodnight")]
    assert runner.calls == [{"name": "trigger_action", "status": "ok"}]


async def test_gate_applies():
    rest = FakeRest(switch="off")
    env = await _runner(rest).run("trigger_action", {"entity_id": "automation.ai_goodnight", "params": {}})
    assert env["error"]["code"] == "ai_disabled"
    assert rest.calls == []


def test_has_respects_enabled_set_and_tier():
    assert _runner(FakeRest(), enabled={"trigger_action"}).has("trigger_action") is True
    assert _runner(FakeRest(), enabled=set()).has("trigger_action") is False
    assert _runner(FakeRest(), enabled=None, allow_actions=False).has("trigger_action") is False
    assert _runner(FakeRest()).has("no_such_tool") is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_degraded_formatters.py tests/test_degraded_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.degraded'`.

- [ ] **Step 3: Implement**

`app/degraded/__init__.py`:

```python
"""Degraded mode: deterministic replies that need no LLM.

The CommandRouter (router.py) answers slash commands at any time and free
text while the LLM is down. It runs the ordinary tools through the adapter, so
the AI-actions gate, audit log and loop guard apply exactly as for the agent.
Replies are English and are never written to the conversation thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Reply:
    text: str
    kind: str = "command"  # "command" (slash command) | "degraded" (LLM down)
    # [{"name": tool_name, "status": "ok" | "error"}] — for the UI's activity lines.
    tool_calls: list[dict] = field(default_factory=list)
```

`app/degraded/tools.py`:

```python
"""Runs registry tools for the router through the same adapter the agent
uses — the pattern EventRunner._run_direct established — so every gate,
audit row and error envelope is identical to an agent call."""

from __future__ import annotations

import json

from app.tools import registry
from app.tools.adapter import LoopGuard, to_structured_tool


class ToolRunner:
    def __init__(self, ctx, enabled: set[str] | None, thread_id: str) -> None:
        self._ctx = ctx
        self._enabled = enabled  # app.state.enabled_tools; None = tier check only
        self._thread_id = thread_id
        self.calls: list[dict] = []

    def has(self, name: str) -> bool:
        definition = registry.get(name)
        if definition is None:
            return False
        if self._enabled is not None:
            return name in self._enabled
        return definition.tier <= self._ctx.settings.max_tier

    async def run(self, name: str, args: dict) -> dict:
        # A fresh guard per call: the router never loops, and two identical
        # commands in a row must both run.
        tool = to_structured_tool(registry.get(name), self._ctx, LoopGuard())
        config = {"configurable": {"thread_id": self._thread_id}}
        raw = await tool.ainvoke(args, config=config)
        envelope = json.loads(raw)
        self.calls.append({"name": name, "status": envelope.get("status", "ok")})
        return envelope
```

`app/degraded/formatters.py`:

```python
"""Tool envelope -> short English text. Pure functions; no I/O."""

from __future__ import annotations

ALIAS_PREFIX = "AI Action:"  # house convention for ai_* script aliases

_ERROR_TEXT = {
    "ha_unreachable": "Couldn't reach Home Assistant.",
    "ha_timeout": "Home Assistant did not answer in time.",
}


def display_action_name(name: str, entity_id: str = "") -> str:
    if name:
        if name.lower().startswith(ALIAS_PREFIX.lower()):
            return name[len(ALIAS_PREFIX):].strip()
        return name
    local = entity_id.split(".", 1)[-1]
    if local.startswith("ai_"):
        local = local[len("ai_"):]
    return local.replace("_", " ")


def _sort_key(row: dict) -> str:
    return display_action_name(row.get("name", ""), row.get("entity_id", "")).lower()


def sorted_action_rows(rows: list[dict]) -> list[dict]:
    """The one ordering /actions numbers and /run <n> resolves against."""
    result = list(rows)
    result.sort(key=_sort_key)
    return result


def _row_label(row: dict) -> str:
    return display_action_name(row.get("name", ""), row.get("entity_id", ""))


def format_actions(rows: list[dict], total: int) -> str:
    if not rows:
        return "No AI actions are available."
    lines = ["Actions:"]
    number = 1
    for row in rows:
        lines.append(f"{number}. {_row_label(row)}")
        number += 1
    if total > len(rows):
        lines.append(f"(showing {len(rows)} of {total})")
    lines.append("Run one with /run <number or name>.")
    return "\n".join(lines)


def format_did_you_mean(candidates: list[dict], rows: list[dict]) -> str:
    lines = ["Did you mean:"]
    for candidate in candidates:
        number = rows.index(candidate) + 1
        lines.append(f"{number}. {_row_label(candidate)}")
    lines.append("Reply /run <number>.")
    return "\n".join(lines)


def format_error(envelope: dict) -> str:
    error = envelope.get("error") or {}
    code = error.get("code") or ""
    if code in _ERROR_TEXT:
        return _ERROR_TEXT[code]
    return error.get("message") or "Something went wrong."


def format_trigger(envelope: dict, name: str, params: dict) -> str:
    # Same wording as the fast path's templated confirmation.
    if envelope.get("status") != "ok":
        return format_error(envelope)
    if params:
        parts = []
        for key, value in params.items():
            parts.append(f"{key}: {value}")
        return f"Done — triggered {name} ({', '.join(parts)})."
    return f"Done — triggered {name}."


def format_notes(envelope: dict) -> str:
    if envelope.get("status") != "ok":
        return format_error(envelope)
    rows = (envelope.get("data") or {}).get("rows") or []
    if not rows:
        return "No scheduled tasks."
    lines = ["Scheduled tasks:"]
    for row in rows:
        lines.append(f"#{row['id']} — {row.get('when', '')}: {row.get('instruction', '')}")
    lines.append("Cancel one with /cancel <id>.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_degraded_formatters.py tests/test_degraded_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/degraded tests/test_degraded_formatters.py tests/test_degraded_tools.py
git commit -m "feat(degraded): Reply, ToolRunner and formatters"
```

---

### Task 7: Fuzzy action matcher

**Files:**
- Create: `app/degraded/matcher.py`
- Test: `tests/test_degraded_matcher.py`

**Interfaces:**
- Produces: `Match(winner: dict | None, candidates: list[dict])` (frozen dataclass), `normalise(text: str) -> list[str]`, `score(query: str, name: str) -> float`, `match_action(text: str, rows: list[dict]) -> Match`, and the constants `FIRE_SCORE = 0.80`, `FIRE_MARGIN = 0.15`, `SUGGEST_SCORE = 0.50`, `MAX_CANDIDATES = 3`.

The scores in these tests were computed with this exact algorithm during planning. "turn on the living room lights" scores 1.0 against "Living Room Lights On" and 0.732 against "Living Room Lights Off". "living room lights" scores 0.819 and 0.81. "vacuum" scores 0.582 and 0.567. "tuled põlema elutoas" scores 1.0.

- [ ] **Step 1: Write the failing tests** — `tests/test_degraded_matcher.py`

```python
from app.degraded.matcher import match_action, normalise, score

ROWS = [
    {"entity_id": "script.ai_action_lights_on", "name": "AI Action: Living Room Lights On"},
    {"entity_id": "script.ai_action_lights_off", "name": "AI Action: Living Room Lights Off"},
    {"entity_id": "script.ai_action_stop_vacuum", "name": "AI Action: Stop Vacuum"},
    {"entity_id": "script.ai_action_start_vacuum", "name": "AI Action: Start Vacuum"},
    {"entity_id": "automation.ai_goodnight", "name": "AI Action: Goodnight"},
    {"entity_id": "script.ai_tuled", "name": "Tuled põlema elutoas"},
]


def test_normalise_drops_filler_and_prefix_but_keeps_on_off_start_stop():
    assert normalise("AI Action: Turn ON the lights, please!") == ["on", "lights"]
    assert normalise("start the vacuum") == ["start", "vacuum"]


def test_score_is_order_insensitive():
    assert score("lights living room on", "Living Room Lights On") == 1.0


def test_clear_winner_fires():
    match = match_action("turn on the living room lights", ROWS)
    assert match.winner["entity_id"] == "script.ai_action_lights_on"


def test_on_and_off_are_distinguished():
    match = match_action("lights on in the living room please", ROWS)
    assert match.winner["entity_id"] == "script.ai_action_lights_on"


def test_start_and_stop_are_distinguished():
    assert match_action("stop the vacuum", ROWS).winner["entity_id"] == "script.ai_action_stop_vacuum"
    assert match_action("start the vacuum", ROWS).winner["entity_id"] == "script.ai_action_start_vacuum"


def test_near_tie_gives_candidates():
    match = match_action("living room lights", ROWS)
    assert match.winner is None
    ids = []
    for row in match.candidates:
        ids.append(row["entity_id"])
    assert ids[:2] == ["script.ai_action_lights_on", "script.ai_action_lights_off"]


def test_single_word_ambiguous_gives_candidates():
    match = match_action("vacuum", ROWS)
    assert match.winner is None
    assert len(match.candidates) == 2


def test_non_english_name_matches_original_text():
    assert match_action("tuled põlema elutoas", ROWS).winner["entity_id"] == "script.ai_tuled"


def test_unrelated_text_matches_nothing():
    match = match_action("what's the temperature", ROWS)
    assert match.winner is None
    assert match.candidates == []


def test_empty_rows():
    match = match_action("lights", [])
    assert match.winner is None
    assert match.candidates == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_degraded_matcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.degraded.matcher'`.

- [ ] **Step 3: Implement** — `app/degraded/matcher.py`

```python
"""Fuzzy match of free text against action names, for when the LLM is down.

Never guesses on a write: an action fires only with one clear winner (high
score AND a clear margin over the runner-up). Anything less returns a short
candidate list for the user to pick from with /run <n>.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

FIRE_SCORE = 0.80
FIRE_MARGIN = 0.15
SUGGEST_SCORE = 0.50
MAX_CANDIDATES = 3

_PREFIX = "ai action:"
# Words that carry no meaning on their own. on/off and start/stop are NOT
# here: they are exactly what tells "Lights On" from "Lights Off".
_FILLER = frozenset({
    "please", "can", "could", "you", "the", "a", "an",
    "turn", "switch", "run", "do", "my", "to",
})
_NON_WORD = re.compile(r"[^\w\s]")


@dataclass(frozen=True)
class Match:
    winner: dict | None
    candidates: list[dict] = field(default_factory=list)


def normalise(text: str) -> list[str]:
    lowered = text.lower().strip()
    if lowered.startswith(_PREFIX):
        lowered = lowered[len(_PREFIX):]
    lowered = _NON_WORD.sub(" ", lowered)
    words = []
    for word in lowered.split():
        if word not in _FILLER:
            words.append(word)
    return words


def score(query: str, name: str) -> float:
    query_words = set(normalise(query))
    name_words = set(normalise(name))
    if not query_words or not name_words:
        return 0.0
    jaccard = len(query_words & name_words) / len(query_words | name_words)
    # Sorted, so word order ("lights on" vs "on lights") does not matter.
    query_text = " ".join(sorted(query_words))
    name_text = " ".join(sorted(name_words))
    ratio = difflib.SequenceMatcher(None, query_text, name_text).ratio()
    return 0.6 * jaccard + 0.4 * ratio


def _row_name(row: dict) -> str:
    name = row.get("name") or ""
    if name:
        return name
    return (row.get("entity_id") or "").split(".", 1)[-1].replace("_", " ")


def _by_score(pair: tuple[float, int]) -> tuple[float, int]:
    value, index = pair
    return (-value, index)  # best first; ties keep input order


def match_action(text: str, rows: list[dict]) -> Match:
    scored = []
    index = 0
    for row in rows:
        scored.append((score(text, _row_name(row)), index))
        index += 1
    if not scored:
        return Match(winner=None)
    scored.sort(key=_by_score)
    best_score, best_index = scored[0]
    second_score = 0.0
    if len(scored) > 1:
        second_score = scored[1][0]
    if best_score >= FIRE_SCORE and best_score - second_score >= FIRE_MARGIN:
        return Match(winner=rows[best_index])
    candidates = []
    for value, row_index in scored[:MAX_CANDIDATES]:
        if value >= SUGGEST_SCORE:
            candidates.append(rows[row_index])
    return Match(winner=None, candidates=candidates)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_degraded_matcher.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add app/degraded/matcher.py tests/test_degraded_matcher.py
git commit -m "feat(degraded): fuzzy action matcher with fire bar and margin"
```

---

### Task 8: Slash commands

**Files:**
- Create: `app/degraded/commands.py`
- Test: `tests/test_degraded_commands.py`

**Interfaces:**
- Consumes: `ToolRunner` and the formatters (Task 6); `match_action` (Task 7); `ServiceHealth` (Task 2).
- Produces, in `app.degraded.commands`:
  - `HELP_TEXT: str`, `NOT_AVAILABLE: str`;
  - `llm_status_line(llm_health) -> str`;
  - `parse_run_args(arg: str) -> tuple[str, dict]`;
  - `async list_action_rows(runner) -> list[dict] | str` (sorted rows, or an error text);
  - `async run_action_row(row: dict, params: dict, runner) -> str`;
  - `async cmd_help(arg, runner, llm_health) -> str`;
  - `async cmd_actions(arg, runner) -> str`;
  - `async cmd_run(arg, runner) -> str`;
  - `async cmd_notes(arg, runner) -> str`;
  - `async cmd_cancel(arg, runner) -> str`.

- [ ] **Step 1: Write the failing tests** — `tests/test_degraded_commands.py`

```python
import pytest

from app.config import Settings
from app.degraded.commands import (
    cmd_actions,
    cmd_cancel,
    cmd_help,
    cmd_notes,
    cmd_run,
    parse_run_args,
)
from app.degraded.tools import ToolRunner
from app.health import ServiceHealth
from app.memory.store import NoteStore
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {"entity_id": "script.ai_action_lights_on", "state": "off",
     "attributes": {"friendly_name": "AI Action: Living Room Lights On"}},
    {"entity_id": "script.ai_action_lights_off", "state": "off",
     "attributes": {"friendly_name": "AI Action: Living Room Lights Off"}},
    {"entity_id": "script.ai_action_dim", "state": "off",
     "attributes": {"friendly_name": "AI Action: Dim"}},
    {"entity_id": "automation.ai_goodnight", "state": "on",
     "attributes": {"friendly_name": "Goodnight"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
]

DIM_FIELDS = {"fields": {"level": {"required": True, "selector": {"number": {"min": 1, "max": 100}}}}}


class FakeRest:
    def __init__(self, switch="on"):
        self.switch = switch
        self.calls = []

    async def list_states(self):
        return STATES

    async def get_state(self, entity_id):
        if entity_id == "input_boolean.ai_triggered_actions":
            return {"entity_id": entity_id, "state": self.switch, "attributes": {}}
        return {"entity_id": entity_id, "state": "off", "attributes": {}}

    async def get_script_config(self, object_id):
        if object_id == "ai_action_dim":
            return DIM_FIELDS
        return {}

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id, data))
        return []


@pytest.fixture(autouse=True)
def tools():
    registry._reset_for_tests()
    registry.load_all((
        "app.tools.read.list_actions",
        "app.tools.action.trigger_action",
        "app.tools.memory.list_scheduled",
        "app.tools.memory.cancel_scheduled",
    ))
    yield
    registry._reset_for_tests()


@pytest.fixture
def store():
    s = NoteStore("")
    yield s
    s.close()


def _runner(rest, store=None, allow_actions=True):
    settings = Settings(_env_file=None, allow_actions=allow_actions)
    ctx = ToolContext(settings=settings, rest=rest, ws=None, notes=store)
    return ToolRunner(ctx, None, "t1")


def test_parse_run_args():
    assert parse_run_args("Dim level=30") == ("Dim", {"level": 30})
    assert parse_run_args("2") == ("2", {})
    assert parse_run_args("Set mode mode=eco") == ("Set mode", {"mode": "eco"})
    assert parse_run_args("") == ("", {})


async def test_help_lists_commands_and_llm_status():
    async def probe():
        return False

    health = ServiceHealth("llm", probe)
    health.mark_down("refused")
    text = await cmd_help("", _runner(FakeRest()), health)
    assert "/actions" in text and "/run" in text and "/notes" in text and "/cancel" in text
    assert "LLM: offline since" in text


async def test_actions_lists_sorted_numbered_names():
    text = await cmd_actions("", _runner(FakeRest()))
    assert "1. Dim" in text
    assert "2. Goodnight" in text
    assert "3. Living Room Lights Off" in text
    assert "light.kitchen" not in text


async def test_run_by_number():
    rest = FakeRest()
    text = await cmd_run("2", _runner(rest))
    assert text == "Done — triggered Goodnight."
    assert rest.calls[0][:3] == ("automation", "trigger", "automation.ai_goodnight")


async def test_run_by_name_clear_winner():
    rest = FakeRest()
    text = await cmd_run("living room lights on", _runner(rest))
    assert text == "Done — triggered Living Room Lights On."
    assert rest.calls[0][:2] == ("script", "ai_action_lights_on")


async def test_run_ambiguous_name_lists_candidates_with_actions_numbers():
    rest = FakeRest()
    text = await cmd_run("living room lights", _runner(rest))
    assert text.startswith("Did you mean:")
    assert "4. Living Room Lights On" in text
    assert "3. Living Room Lights Off" in text
    assert rest.calls == []


async def test_run_missing_required_params_explains():
    rest = FakeRest()
    text = await cmd_run("Dim", _runner(rest))
    assert text == "Dim needs: level. Example: /run Dim level=…"
    assert rest.calls == []


async def test_run_with_params():
    rest = FakeRest()
    text = await cmd_run("Dim level=30", _runner(rest))
    assert text == "Done — triggered Dim (level: 30)."
    assert rest.calls[0] == ("script", "ai_action_dim", None, {"level": 30})


async def test_run_refused_when_ai_switch_off():
    rest = FakeRest(switch="off")
    text = await cmd_run("2", _runner(rest))
    assert text.startswith("AI-triggered actions are turned off")
    assert rest.calls == []


async def test_run_unknown_number_and_no_match():
    assert await cmd_run("9", _runner(FakeRest())) == "No action number 9. See /actions."
    assert await cmd_run("espresso", _runner(FakeRest())) == "No action matches “espresso”. See /actions."


async def test_run_not_available_without_actions():
    text = await cmd_run("2", _runner(FakeRest(), allow_actions=False))
    assert text == "Not available in this home."


async def test_notes_and_cancel(store):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    note_id = store.add_state_note(
        entity_id="vacuum.robot", to_state="cleaning", instruction="Stop the vacuum.",
        instruction_original=None, kind="action", language="en",
        expires_at=now + timedelta(hours=24), now=now, source_thread_id="t1",
        action_entity_id="script.ai_action_stop_vacuum", reminder=None,
    )
    runner = _runner(FakeRest(), store)
    listed = await cmd_notes("", runner)
    assert f"#{note_id}" in listed and "Stop the vacuum." in listed
    assert await cmd_cancel(str(note_id), runner) == f"Cancelled task #{note_id}."
    assert await cmd_notes("", runner) == "No scheduled tasks."
    assert await cmd_cancel("abc", runner) == "Usage: /cancel <id>. See /notes."
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_degraded_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.degraded.commands'`.

- [ ] **Step 3: Implement** — `app/degraded/commands.py`

```python
"""Slash commands. Each takes the text after the command word and a
ToolRunner, and returns English text. run_action_row / list_action_rows are
shared with the natural-language fallback in router.py."""

from __future__ import annotations

from app.degraded.formatters import (
    display_action_name,
    format_actions,
    format_did_you_mean,
    format_error,
    format_notes,
    format_trigger,
    sorted_action_rows,
)
from app.degraded.matcher import match_action
from app.health import DOWN, UP

NOT_AVAILABLE = "Not available in this home."

HELP_TEXT = "\n".join([
    "Commands:",
    "/actions — list the actions I can run",
    "/run <number or name> [key=value …] — run one",
    "/notes — list scheduled tasks",
    "/cancel <id> — cancel a scheduled task",
    "/help — this list",
])


def llm_status_line(llm_health) -> str:
    if llm_health is None:
        return "LLM: status unknown"
    if llm_health.state == UP:
        return "LLM: up"
    if llm_health.state == DOWN:
        return f"LLM: offline since {llm_health.since_hhmm()}"
    return "LLM: status unknown"


def _coerce(value: str):
    digits = value
    if digits.startswith("-"):
        digits = digits[1:]
    if digits.isdigit():
        return int(value)
    return value


def parse_run_args(arg: str) -> tuple[str, dict]:
    """'Dim level=30' -> ('Dim', {'level': 30}). Tokens with '=' are params;
    the rest, joined, is the action number or name."""
    name_parts = []
    params = {}
    for token in arg.split():
        if "=" in token and not token.startswith("="):
            key, value = token.split("=", 1)
            params[key] = _coerce(value)
        else:
            name_parts.append(token)
    return " ".join(name_parts), params


async def list_action_rows(runner) -> list[dict] | str:
    envelope = await runner.run("list_actions", {})
    if envelope.get("status") != "ok":
        return format_error(envelope)
    rows = (envelope.get("data") or {}).get("rows") or []
    return sorted_action_rows(rows)


async def run_action_row(row: dict, params: dict, runner) -> str:
    name = display_action_name(row.get("name", ""), row.get("entity_id", ""))
    required = (row.get("params") or {}).get("required") or []
    missing = []
    for key in required:
        if key not in params:
            missing.append(key)
    if missing:
        example_parts = []
        for key in missing:
            example_parts.append(f"{key}=…")
        return f"{name} needs: {', '.join(missing)}. Example: /run {name} {' '.join(example_parts)}"
    envelope = await runner.run(
        "trigger_action", {"entity_id": row["entity_id"], "params": params},
    )
    return format_trigger(envelope, name, params)


async def cmd_help(arg: str, runner, llm_health) -> str:
    return HELP_TEXT + "\n" + llm_status_line(llm_health)


async def cmd_actions(arg: str, runner) -> str:
    if not runner.has("list_actions"):
        return NOT_AVAILABLE
    envelope = await runner.run("list_actions", {})
    if envelope.get("status") != "ok":
        return format_error(envelope)
    data = envelope.get("data") or {}
    rows = sorted_action_rows(data.get("rows") or [])
    return format_actions(rows, total=data.get("total", len(rows)))


async def cmd_run(arg: str, runner) -> str:
    if not runner.has("list_actions") or not runner.has("trigger_action"):
        return NOT_AVAILABLE
    name, params = parse_run_args(arg)
    if not name:
        return "Usage: /run <number or name> [key=value …]. See /actions."
    rows = await list_action_rows(runner)
    if isinstance(rows, str):
        return rows
    if name.isdigit():
        number = int(name)
        if number < 1 or number > len(rows):
            return f"No action number {number}. See /actions."
        return await run_action_row(rows[number - 1], params, runner)
    match = match_action(name, rows)
    if match.winner is not None:
        return await run_action_row(match.winner, params, runner)
    if match.candidates:
        return format_did_you_mean(match.candidates, rows)
    return f"No action matches “{name}”. See /actions."


async def cmd_notes(arg: str, runner) -> str:
    if not runner.has("list_scheduled"):
        return NOT_AVAILABLE
    return format_notes(await runner.run("list_scheduled", {}))


async def cmd_cancel(arg: str, runner) -> str:
    if not runner.has("cancel_scheduled"):
        return NOT_AVAILABLE
    text = arg.strip()
    if not text.isdigit():
        return "Usage: /cancel <id>. See /notes."
    envelope = await runner.run("cancel_scheduled", {"id": int(text)})
    if envelope.get("status") != "ok":
        return format_error(envelope)
    return f"Cancelled task #{text}."
```

Note: the tests run with `enabled=None`, so `has()` falls back to the tier check. The memory tools are READ tier and always pass it. In the real app, `enabled_tools` decides (Task 9 passes it in).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_degraded_commands.py -v`
Expected: PASS (13 tests). If `test_notes_and_cancel` fails on the `add_state_note` signature, copy the exact call from `tests/test_event_runner.py::_add`, which is the source of truth for that signature.

- [ ] **Step 5: Commit**

```bash
git add app/degraded/commands.py tests/test_degraded_commands.py
git commit -m "feat(degraded): /help /actions /run /notes /cancel"
```

---

### Task 9: `CommandRouter` and the natural-language fallback

**Files:**
- Create: `app/degraded/router.py`
- Test: `tests/test_degraded_router.py`

**Interfaces:**
- Consumes: the `commands.py` functions (Task 8); `match_action` (Task 7); `ToolRunner`, `Reply`, `format_trigger`, `format_did_you_mean`, `display_action_name` (Task 6); `has_deferral_cue` from `app.agent.middleware.fast_path_middleware`; `is_fast_path_outage` (Task 3).
- Produces, in `app.degraded.router`:
  - `CommandRouter(ctx, enabled: set[str] | None, llm_health, fast_path=None, fp_health=None)`;
  - `CommandRouter.is_command(text: str) -> bool` (staticmethod);
  - `async handle(text: str, thread_id: str) -> Reply | None`;
  - `offline_message(llm_health) -> str`;
  - `DEFERRAL_REPLY: str`.

- [ ] **Step 1: Write the failing tests** — `tests/test_degraded_router.py`

```python
import httpx
import pytest

from app.config import Settings
from app.degraded.router import DEFERRAL_REPLY, CommandRouter
from app.fast_path.backend import Decision
from app.health import ServiceHealth
from app.needle.menu import Menu, MenuItem
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {"entity_id": "script.ai_action_lights_on", "state": "off",
     "attributes": {"friendly_name": "AI Action: Living Room Lights On"}},
    {"entity_id": "script.ai_action_lights_off", "state": "off",
     "attributes": {"friendly_name": "AI Action: Living Room Lights Off"}},
]


class FakeRest:
    def __init__(self):
        self.calls = []

    async def list_states(self):
        return STATES

    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    async def get_script_config(self, object_id):
        return {}

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service))
        return []


@pytest.fixture(autouse=True)
def tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.list_actions", "app.tools.action.trigger_action"))
    yield
    registry._reset_for_tests()


async def _never():
    return False


def _health(down: bool):
    health = ServiceHealth("llm", _never)
    if down:
        health.mark_down("refused")
    return health


def _router(rest, llm_down=True, fast_path=None, fp_health=None):
    settings = Settings(_env_file=None, allow_actions=True)
    ctx = ToolContext(settings=settings, rest=rest, ws=None)
    return CommandRouter(ctx, None, _health(llm_down), fast_path=fast_path, fp_health=fp_health)


def test_is_command():
    assert CommandRouter.is_command("  /help")
    assert not CommandRouter.is_command("help")


async def test_slash_command_handled_even_when_llm_up():
    reply = await _router(FakeRest(), llm_down=False).handle("/help", "t1")
    assert reply.kind == "command"
    assert "/actions" in reply.text


async def test_unknown_command_shows_help():
    reply = await _router(FakeRest()).handle("/frobnicate", "t1")
    assert reply.text.startswith("Commands:")


async def test_free_text_passes_through_when_llm_up():
    assert await _router(FakeRest(), llm_down=False).handle("lights on", "t1") is None


async def test_fallback_runs_clear_match():
    rest = FakeRest()
    reply = await _router(rest).handle("turn on the living room lights", "t1")
    assert reply.kind == "degraded"
    assert reply.text == "Done — triggered Living Room Lights On."
    assert rest.calls == [("script", "ai_action_lights_on")]
    names = []
    for call in reply.tool_calls:
        names.append(call["name"])
    assert names == ["list_actions", "trigger_action"]


async def test_fallback_lists_candidates_on_near_tie():
    rest = FakeRest()
    reply = await _router(rest).handle("living room lights", "t1")
    assert reply.text.startswith("Did you mean:")
    assert rest.calls == []


async def test_fallback_refuses_deferred_request():
    rest = FakeRest()
    reply = await _router(rest).handle("when the door opens turn on the living room lights", "t1")
    assert reply.text == DEFERRAL_REPLY
    assert rest.calls == []


async def test_fallback_offline_message_when_nothing_matches():
    reply = await _router(FakeRest()).handle("what's the temperature?", "t1")
    assert "language model is offline" in reply.text
    assert "/actions" in reply.text


class _Menu:
    async def get(self):
        return Menu(items=(MenuItem("script.ai_action_lights_off", "Lights Off", "off"),), signature="s")


class _Backend:
    name = "fake"

    def __init__(self, decision=None, error=None):
        self.decision = decision
        self.error = error
        self.calls = 0

    async def classify(self, message, menu):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.decision


async def test_fallback_uses_fast_path_first():
    rest = FakeRest()
    backend = _Backend(Decision("script.ai_action_lights_off", 0.9))
    reply = await _router(rest, fast_path=(backend, _Menu())).handle("darkness please", "t1")
    assert reply.text == "Done — triggered Lights Off."
    assert rest.calls == [("script", "ai_action_lights_off")]


async def test_fallback_skips_fast_path_while_down():
    backend = _Backend(Decision("script.ai_action_lights_off", 0.9))
    fp_health = _health(down=True)
    reply = await _router(FakeRest(), fast_path=(backend, _Menu()), fp_health=fp_health).handle(
        "darkness please", "t1")
    assert backend.calls == 0
    assert "language model is offline" in reply.text


async def test_fast_path_connection_error_marks_it_down_and_continues():
    backend = _Backend(error=httpx.ConnectError("refused"))
    fp_health = _health(down=False)
    reply = await _router(FakeRest(), fast_path=(backend, _Menu()), fp_health=fp_health).handle(
        "turn on the living room lights", "t1")
    assert fp_health.is_down
    assert reply.text == "Done — triggered Living Room Lights On."
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_degraded_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.degraded.router'`.

- [ ] **Step 3: Implement** — `app/degraded/router.py`

```python
"""The degraded-mode front door, consulted by both chat endpoints.

Slash commands are always handled here. Free text is handled here only while
the LLM is down: deferral guard -> fast path (if its service is up) -> fuzzy
action matcher -> "LLM offline" message. Returns None to let the agent
answer. Replies are English and never touch the conversation thread.
"""

from __future__ import annotations

import logging

from app.agent.llm_health import is_fast_path_outage
from app.agent.middleware.fast_path_middleware import has_deferral_cue
from app.degraded import Reply
from app.degraded.commands import (
    cmd_actions,
    cmd_cancel,
    cmd_help,
    cmd_notes,
    cmd_run,
    list_action_rows,
    run_action_row,
)
from app.degraded.formatters import display_action_name, format_did_you_mean, format_trigger
from app.degraded.matcher import match_action
from app.degraded.tools import ToolRunner

log = logging.getLogger("agent.degraded")

DEFERRAL_REPLY = "I can't schedule things while my language model is offline. Try again later."


def offline_message(llm_health) -> str:
    since = ""
    if llm_health is not None:
        since = f" (since {llm_health.since_hhmm()})"
    return (
        f"My language model is offline{since}, so I can only handle simple commands "
        "right now. Try /actions, /run <name>, or /help."
    )


class CommandRouter:
    def __init__(self, ctx, enabled, llm_health, fast_path=None, fp_health=None) -> None:
        self._ctx = ctx
        self._enabled = enabled
        self._llm_health = llm_health
        self._fast_path = fast_path  # (backend, menu_provider) or None
        self._fp_health = fp_health

    @staticmethod
    def is_command(text: str) -> bool:
        return text.strip().startswith("/")

    async def handle(self, text: str, thread_id: str) -> Reply | None:
        stripped = text.strip()
        runner = ToolRunner(self._ctx, self._enabled, thread_id)
        if stripped.startswith("/"):
            reply_text = await self._command(stripped, runner)
            return Reply(reply_text, "command", runner.calls)
        if self._llm_health is not None and self._llm_health.is_down:
            reply_text = await self._fallback(stripped, runner)
            return Reply(reply_text, "degraded", runner.calls)
        return None

    async def _command(self, text: str, runner) -> str:
        parts = text[1:].split(maxsplit=1)
        name = ""
        if parts:
            name = parts[0].lower()
        arg = ""
        if len(parts) > 1:
            arg = parts[1]
        if name == "actions":
            return await cmd_actions(arg, runner)
        if name == "run":
            return await cmd_run(arg, runner)
        if name == "notes":
            return await cmd_notes(arg, runner)
        if name == "cancel":
            return await cmd_cancel(arg, runner)
        return await cmd_help(arg, runner, self._llm_health)

    async def _fallback(self, text: str, runner) -> str:
        # A request about later must never run now (same rule as the fast path).
        if has_deferral_cue(text):
            return DEFERRAL_REPLY
        fast = await self._try_fast_path(text, runner)
        if fast is not None:
            return fast
        if runner.has("list_actions") and runner.has("trigger_action"):
            rows = await list_action_rows(runner)
            if isinstance(rows, list):
                match = match_action(text, rows)
                if match.winner is not None:
                    return await run_action_row(match.winner, {}, runner)
                if match.candidates:
                    return format_did_you_mean(match.candidates, rows)
        return offline_message(self._llm_health)

    async def _try_fast_path(self, text: str, runner) -> str | None:
        if self._fast_path is None or not runner.has("trigger_action"):
            return None
        if self._fp_health is not None and self._fp_health.is_down:
            return None
        backend, menu_provider = self._fast_path
        try:
            menu = await menu_provider.get()
            if not menu.items:
                return None
            decision = await backend.classify(text, menu)
        except Exception as exc:
            if self._fp_health is not None and is_fast_path_outage(exc):
                self._fp_health.mark_down(exc)
            log.warning("degraded: fast path failed (%s); trying the matcher", exc)
            return None
        threshold = self._ctx.settings.fast_path_confidence_threshold
        if decision.entity_id is None or decision.confidence < threshold:
            return None
        item = None
        for candidate in menu.items:
            if candidate.entity_id == decision.entity_id:
                item = candidate
                break
        if item is None:
            return None
        required = item.parameters.get("required") or []
        for key in required:
            if key not in decision.arguments:
                return None
        envelope = await runner.run(
            "trigger_action", {"entity_id": item.entity_id, "params": decision.arguments},
        )
        return format_trigger(envelope, display_action_name(item.name, item.entity_id),
                              decision.arguments)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_degraded_router.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add app/degraded/router.py tests/test_degraded_router.py
git commit -m "feat(degraded): CommandRouter with natural-language fallback chain"
```

---

### Task 10: Edge wiring: both chat endpoints, streaming outage, thread hygiene

**Files:**
- Modify: `app/agent/history.py` (add `UNANSWERED_MARKER`, `close_dangling_turn`; hide the marker in `display_transcript`)
- Modify: `app/agent/streaming.py` (`stream_events`)
- Modify: `app/main.py` (`ChatResponse`, the lifespan builds the router, `/api/chat`, `/api/chat/stream`)
- Test: `tests/test_chat_degraded.py`, `tests/test_streaming.py`, `tests/test_thread_history.py`

**Interfaces:**
- Consumes: `CommandRouter`, `offline_message` (Task 9); `Reply` (Task 6); `is_llm_outage` (Task 3); `app.state.llm_health` and `app.state.fp_health` (Task 5).
- Produces:
  - `app.state.router: CommandRouter`;
  - `ChatResponse.degraded: bool = False`;
  - the SSE `done` event may carry `"degraded": true`;
  - `stream_events` yields `{"type": "llm_unavailable", "error": str}` on an outage;
  - `UNANSWERED_MARKER: str` and `async close_dangling_turn(agent, thread_id) -> None` in `app.agent.history`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_streaming.py`:

```python
async def test_outage_yields_llm_unavailable_instead_of_raising():
    import httpx

    agent = FakeAgent([(AIMessageChunk(content="Par"), {}), httpx.ConnectError("refused")])
    events = await _collect(agent)
    assert events[-1]["type"] == "llm_unavailable"
    assert "refused" in events[-1]["error"]


async def test_non_outage_errors_still_raise():
    import pytest

    agent = FakeAgent([ValueError("bug")])
    with pytest.raises(ValueError):
        await _collect(agent)
```

Append to `tests/test_thread_history.py`:

```python
async def test_close_dangling_turn_appends_marker(monkeypatch):
    import httpx
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from app.agent import factory as factory_mod
    from app.agent.history import UNANSWERED_MARKER, close_dangling_turn, display_transcript
    from app.config import Settings
    from app.tools import registry
    from app.tools.context import ToolContext

    class Down(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        async def _agenerate(self, *args, **kwargs):
            raise httpx.ConnectError("refused")

    class Rest:
        async def get_state(self, entity_id):
            return {}

        async def list_states(self):
            return []

    registry._reset_for_tests()
    settings = Settings(_env_file=None)
    monkeypatch.setattr(factory_mod, "build_llm", lambda s: Down(responses=[AIMessage("x")]))
    agent = factory_mod.build_agent(settings, ToolContext(settings=settings, rest=Rest(), ws=None))
    config = {"configurable": {"thread_id": "t1"}}
    try:
        await agent.ainvoke({"messages": [{"role": "user", "content": "hi"}]}, config=config)
    except httpx.ConnectError:
        pass
    await close_dangling_turn(agent, "t1")
    await close_dangling_turn(agent, "t1")  # idempotent: last message is no longer human
    messages = (await agent.aget_state(config)).values["messages"]
    assert [m.type for m in messages] == ["human", "ai"]
    assert messages[-1].content == UNANSWERED_MARKER
    assert display_transcript(messages) == [{"role": "user", "text": "hi"}]
    registry._reset_for_tests()
```

Create `tests/test_chat_degraded.py`:

```python
import json

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk

from app.config import Settings
from app.main import create_app


def _settings():
    return Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        llm_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
    )


class OutageAgent:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        raise httpx.ConnectError("refused")

    async def astream(self, payload, config=None, stream_mode=None):
        self.calls += 1
        yield (AIMessageChunk(content="Part"), {})
        raise httpx.ConnectError("refused")


class CountingAgent:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, payload, config=None):
        self.calls += 1
        return {"messages": [AIMessage(content="agent reply")]}


class CountingLanguage:
    enabled = True

    def __init__(self):
        self.inbound_calls = 0

    async def inbound(self, text, thread_id):
        self.inbound_calls += 1
        raise AssertionError("lang-mt must not be called while the LLM is down")

    async def outbound(self, reply_en, inbound):
        raise AssertionError("lang-mt must not be called while the LLM is down")


def _sse_events(text):
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if frame.startswith("data:"):
            events.append(json.loads(frame[len("data:"):]))
    return events


def _stop_probes(client):
    client.portal.call(client.app.state.llm_health.stop)


def test_chat_outage_returns_degraded_reply_and_marks_down():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        client.app.state.agent = OutageAgent()
        resp = client.post("/api/chat", json={"message": "what's the temperature?"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["degraded"] is True
        assert "language model is offline" in body["reply"]
        assert client.app.state.llm_health.state == "down"


def test_message_while_down_never_calls_agent_or_lang_mt():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        agent = CountingAgent()
        language = CountingLanguage()
        client.app.state.agent = agent
        client.app.state.language = language
        client.app.state.llm_health.mark_down("refused")
        body = client.post("/api/chat", json={"message": "hello"}).json()
        assert body["degraded"] is True
        assert agent.calls == 0
        assert language.inbound_calls == 0


def test_agent_called_again_after_recovery():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        agent = CountingAgent()
        client.app.state.agent = agent
        client.app.state.llm_health.mark_down("refused")
        client.portal.call(client.app.state.llm_health.mark_up)
        body = client.post("/api/chat", json={"message": "hello"}).json()
        assert body["reply"] == "agent reply"
        assert body["degraded"] is False
        assert agent.calls == 1


def test_slash_command_never_reaches_agent():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        agent = CountingAgent()
        client.app.state.agent = agent
        body = client.post("/api/chat", json={"message": "/help"}).json()
        assert body["degraded"] is True
        assert "/actions" in body["reply"]
        assert agent.calls == 0


def test_router_crash_returns_offline_reply():
    class Exploding:
        async def handle(self, text, thread_id):
            raise RuntimeError("router bug")

    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        client.app.state.router = Exploding()
        resp = client.post("/api/chat", json={"message": "/help"})
        assert resp.status_code == 200
        assert "language model is offline" in resp.json()["reply"]


def test_stream_outage_ends_with_degraded_done():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        client.app.state.agent = OutageAgent()
        resp = client.post("/api/chat/stream", json={"message": "what's the temperature?"})
        events = _sse_events(resp.text)
        assert events[-1]["type"] == "done"
        assert events[-1]["degraded"] is True
        assert "language model is offline" in events[-1]["reply"]
        assert client.app.state.llm_health.state == "down"


def test_stream_slash_command():
    app = create_app(_settings())
    with TestClient(app) as client:
        _stop_probes(client)
        resp = client.post("/api/chat/stream", json={"message": "/help"})
        events = _sse_events(resp.text)
        assert events[-1]["type"] == "done"
        assert events[-1]["degraded"] is True
        assert "/actions" in events[-1]["reply"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_chat_degraded.py tests/test_streaming.py tests/test_thread_history.py -v`
Expected: FAIL: `ImportError` for `UNANSWERED_MARKER`; streaming re-raises `ConnectError`; `/api/chat` returns 500; `degraded` key missing.

- [ ] **Step 3: Implement `app/agent/history.py`.** Add the import `import logging`, change the messages import to `from langchain_core.messages import AIMessage, HumanMessage`, and add:

```python
log = logging.getLogger("agent.history")

# Closes a turn whose model call failed because the LLM was unreachable, so
# the thread never holds two user messages in a row. Hidden from the UI.
UNANSWERED_MARKER = "(not answered: language model unavailable)"


async def close_dangling_turn(agent, thread_id: str) -> None:
    """If the thread ends with a user message (its model call failed),
    append the marker as the model's reply. Best-effort; never raises."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await agent.aget_state(config)
        messages = (snapshot.values or {}).get("messages", [])
        if messages and isinstance(messages[-1], HumanMessage):
            await agent.aupdate_state(
                config, {"messages": [AIMessage(content=UNANSWERED_MARKER)]}, as_node="model",
            )
    except Exception:
        log.warning("could not close dangling turn for thread %s", thread_id, exc_info=True)
```

In `display_transcript`, in the `AIMessage` branch, skip the marker:

```python
        elif isinstance(message, AIMessage):
            if message.content == UNANSWERED_MARKER:
                continue
            _, text = split_thinking(message)
```

(`as_node="model"` is correct for the installed `create_agent` graph: it was verified during planning that the nodes are `model` and `tools`, and that the next turn runs normally afterwards.)

- [ ] **Step 4: Implement `app/agent/streaming.py`.** Add `from app.agent.llm_health import is_llm_outage`. After the `except GraphRecursionError:` block in `stream_events`, add:

```python
    except Exception as exc:
        # An unreachable LLM is not a crash: tell the endpoint, which answers
        # in degraded mode. Anything else is a real bug and propagates.
        if not is_llm_outage(exc):
            raise
        yield {"type": "llm_unavailable", "error": str(exc) or type(exc).__name__}
        return
```

- [ ] **Step 5: Implement `app/main.py`.**

Add imports:

```python
from app.agent.history import close_dangling_turn
from app.agent.llm_health import is_llm_outage
from app.degraded import Reply
from app.degraded.router import CommandRouter, offline_message
```

In `ChatResponse`, add `degraded: bool = False` with the comment `# True when answered without the LLM (command or degraded mode); the reply is English.`

In the lifespan, right after `app.state.agent = agent` (inside `open_checkpointer`), add:

```python
                app.state.router = CommandRouter(
                    ctx, enabled, llm_health, fast_path=fast_path, fp_health=fp_health,
                )
```

Inside `create_app`, next to `_record_native_turn`, add these helpers:

```python
    def _wants_router(message: str) -> bool:
        if CommandRouter.is_command(message):
            return True
        llm_health = getattr(app.state, "llm_health", None)
        return llm_health is not None and llm_health.is_down

    async def _router_reply(message: str, thread_id: str) -> Reply:
        """The router's answer, or the generic offline reply if it fails —
        a router bug must never turn into a 500."""
        llm_health = getattr(app.state, "llm_health", None)
        try:
            reply = await app.state.router.handle(message, thread_id)
        except Exception:
            log.exception("degraded: router failed")
            reply = None
        if reply is None:
            reply = Reply(offline_message(llm_health), "degraded")
        return reply

    async def _degraded_after_outage(exc: Exception, message: str, thread_id: str) -> Reply:
        llm_health = getattr(app.state, "llm_health", None)
        if llm_health is not None:
            llm_health.mark_down(exc)
        await close_dangling_turn(app.state.agent, thread_id)
        return await _router_reply(message, thread_id)
```

In `/api/chat`, inside `with tracer.start_as_current_span(...)` and **before** `inbound = await language.inbound(...)`, add:

```python
            if _wants_router(req.message):
                reply = await _router_reply(req.message, req.thread_id)
                try:
                    span.set_attribute(C.GOSLING_PATH, reply.kind)
                    span.set_attribute(C.GOSLING_OUTCOME, "ok")
                except Exception:
                    pass
                return ChatResponse(reply=reply.text, degraded=True, language="en")
```

Replace the final `except Exception:` block of `/api/chat` with:

```python
            except Exception as exc:
                if is_llm_outage(exc):
                    reply = await _degraded_after_outage(exc, req.message, req.thread_id)
                    try:
                        span.set_attribute(C.GOSLING_OUTCOME, "llm_unavailable")
                        span.set_attribute(C.GOSLING_PATH, reply.kind)
                    except Exception:
                        pass
                    return ChatResponse(reply=reply.text, request_id=request_id,
                                        degraded=True, language="en")
                try:
                    span.set_attribute(C.GOSLING_OUTCOME, "error")
                    span.set_attribute(C.GOSLING_PATH, "agent")
                except Exception:
                    pass
                raise
```

The fallback deliberately uses `req.message`, the **original** text: action names may be non-English, and lang-mt may share the LLM's machine.

In `/api/chat/stream`, in `sse()`:
1. Move `outcome = "ok"` to the very top of `sse()`, before the outer `try:`, so the `finally` always sees it.
2. Add a nested helper inside `sse()`:

```python
        def _reply_events(reply: Reply, request_id) -> list[str]:
            frames = []
            for call in reply.tool_calls:
                frames.append(_sse({"type": "tool_call", "name": call["name"], "args": {}}))
                frames.append(_sse({"type": "tool_result", "name": call["name"],
                                    "status": call["status"]}))
            frames.append(_sse({"type": "done", "reply": reply.text, "reply_en": None,
                                "language": "en", "degraded": True, "request_id": request_id}))
            return frames
```

3. Right after `request_id = format(...)` is computed, which needs the span, move the `trace_id`/`request_id` lines up so they come **before** `inbound = await language.inbound(...)`. Then insert:

```python
                    if _wants_router(req.message):
                        reply = await _router_reply(req.message, req.thread_id)
                        try:
                            span.set_attribute(C.GOSLING_PATH, reply.kind)
                        except Exception:
                            pass
                        for frame in _reply_events(reply, request_id):
                            yield frame
                        return
```

4. In the `async for event in stream_events(...)` loop, add a branch before the final `else:`:

```python
                        elif event.get("type") == "llm_unavailable":
                            outcome = "llm_unavailable"
                            reply = await _degraded_after_outage(
                                ConnectionError(event.get("error", "")), req.message, req.thread_id,
                            )
                            for frame in _reply_events(reply, request_id):
                                yield frame
```

The frontend already replaces streamed tokens with the `done` text, so a partial "Part" token is overwritten.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_chat_degraded.py tests/test_streaming.py tests/test_thread_history.py tests/test_main.py -v`, then `uv run pytest -q`
Expected: PASS, with exactly 1 warning.

- [ ] **Step 7: Commit**

```bash
git add app/agent/history.py app/agent/streaming.py app/main.py tests/test_chat_degraded.py tests/test_streaming.py tests/test_thread_history.py
git commit -m "feat(degraded): route commands and LLM outages at the chat edge"
```

---

### Task 11: Frontend: offline banner, degraded bubble, command chips

**Files:**
- Modify: `frontend/index.html`, `frontend/app.js`, `frontend/style.css`

**Interfaces:**
- Consumes: the `/api/health` keys `llm_state`, `llm_since`, `fast_path_state` (Task 5), and the SSE `done.degraded` flag (Task 10).

No automated JS tests exist in this repo. Verify manually (Step 4).

- [ ] **Step 1: HTML.** In `frontend/index.html`, inside `.chat-column` and before `<main id="log">`, add:

```html
      <div id="llm-banner" class="llm-banner" hidden></div>
```

Inside `<form id="row">`, before the `<input>`, add:

```html
        <div id="command-chips" class="command-chips" hidden>
          <button type="button" data-cmd="/actions">/actions</button>
          <button type="button" data-cmd="/run ">/run</button>
          <button type="button" data-cmd="/notes">/notes</button>
          <button type="button" data-cmd="/cancel ">/cancel</button>
          <button type="button" data-cmd="/help">/help</button>
        </div>
```

- [ ] **Step 2: JS.** In `frontend/app.js`:

After the element lookups at the top, add:

```javascript
const llmBanner = document.getElementById('llm-banner');
const commandChips = document.getElementById('command-chips');

// LLM status banner. /api/health reads the server's trackers, so polling is
// cheap. Relative path for HA ingress.
async function refreshHealth() {
  let health;
  try {
    const resp = await fetch('api/health');
    health = await resp.json();
  } catch (err) {
    return;
  }
  if (health.llm_state === 'down') {
    let since = '';
    if (health.llm_since) {
      since = ' since ' + new Date(health.llm_since).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    let text = 'LLM offline' + since + ' — command mode. Type /help.';
    if (health.fast_path_state === 'down') {
      text += ' · fast path offline';
    }
    llmBanner.textContent = text;
    llmBanner.hidden = false;
  } else {
    llmBanner.hidden = true;
  }
}
refreshHealth();
setInterval(refreshHealth, 20000);

input.addEventListener('input', function () {
  commandChips.hidden = !input.value.startsWith('/');
});
commandChips.addEventListener('click', function (e) {
  const cmd = e.target.getAttribute('data-cmd');
  if (!cmd) return;
  input.value = cmd;
  commandChips.hidden = true;
  input.focus();
});
```

In `sendMessage`, in the `evt.type === 'done'` branch, after `bubble.className = 'bubble rendered';`, add:

```javascript
          if (evt.degraded) {
            bubble.classList.add('degraded');
            refreshHealth();
          }
```

Also hide the chips after sending: in the `form` submit handler, add `commandChips.hidden = true;`.

- [ ] **Step 3: CSS.** In `frontend/style.css`, add a token to `:root` and to **both** dark-mode blocks (the `@media (prefers-color-scheme: dark)` block, and the explicit dark block if one exists):

```css
  --warning-soft:         rgba(223, 142, 29, 0.14);  /* yellow, light */
```

In dark mode use `rgba(229, 200, 144, 0.14)`. Then append:

```css
.llm-banner {
  margin: 0.5rem 0;
  padding: 0.45rem 0.75rem;
  border: 1px solid var(--activity);
  border-radius: 8px;
  background: var(--warning-soft);
  color: var(--text);
  font-size: 0.9rem;
}

.bubble.degraded {
  border-left: 3px solid var(--activity);
  background: var(--warning-soft);
}

.command-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  width: 100%;
  margin-bottom: 0.35rem;
}

.command-chips button {
  font: inherit;
  font-size: 0.85rem;
  padding: 0.15rem 0.55rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
}
```

If `#row` is a single-line flex row and the chips break its layout, add `flex-wrap: wrap;` to `#row`.

- [ ] **Step 4: Manual verification**

1. In `.env`, set `LLM_URL` to a dead address (e.g. `http://127.0.0.1:1`), then run `uv run uvicorn app.main:create_app --factory --port 8099`.
2. Open `http://localhost:8099`. Within about 20 s the banner reads "LLM offline since HH:MM — command mode. Type /help."
3. Type `/` and check that the chips appear. Click `/actions` and send it: a numbered list comes back in a tinted bubble.
4. Type "turn on <an action name>": you get either "Done — triggered …" (only when the AI switch is on) or "Did you mean".
5. Restore `LLM_URL`. Within 15 s the banner disappears, and a normal question gets an agent answer.
6. Check dark mode (OS setting): the banner and tint stay readable.

- [ ] **Step 5: Commit**

```bash
git add frontend/index.html frontend/app.js frontend/style.css
git commit -m "feat(ui): LLM offline banner, degraded bubble, command chips"
```

---

### Task 12: Docs

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: README.** Add a section titled `## Degraded mode (LLM offline)` with this text:

```markdown
## Degraded mode (LLM offline)

Gosling probes its LLM server (llama.cpp `/health`, Ollama `/api/version`,
others `/models`) every 15 s while it is down and every 60 s while it is up,
and marks it down immediately when a request fails to connect. While the LLM
is down:

- Requests fail in about 3 s instead of hanging, and the web UI shows an
  "LLM offline — command mode" banner.
- Typed text is matched against your `ai_*` action names. One clear match
  runs (still subject to the AI-actions switch); a near tie returns a
  numbered "did you mean" list. Anything else gets an "LLM offline" reply.
- Requests about later ("when…", "at 18:00…") are refused, never run now.

Slash commands work at any time, LLM or not, and always answer in English:
`/actions`, `/run <number or name> [key=value …]`, `/notes`,
`/cancel <id>`, `/help`. They are not saved in the conversation.

If the fast path is configured, its `/health` is probed the same way; while
it is down, requests skip it instead of waiting out its timeout.
`/api/health` reports `llm_state`, `llm_since`, `fast_path_state` and always
returns HTTP 200 (so the Supervisor watchdog never restart-loops the add-on).
```

- [ ] **Step 2: CLAUDE.md.** Under "Architecture (app/)", add:

```markdown
- `health.py` — `ServiceHealth`: generic up/down tracker for one HTTP
  dependency (probe loop 15 s down / 60 s up + passive `mark_down`). Two
  instances on `app.state`: `llm_health`, `fp_health` (fast path, optional).
- `agent/llm_health.py` — provider-specific probes, `is_llm_outage(exc)`
  (connection/timeout/5xx = outage; bad output is not), and re-resolving an
  `auto` provider that could not be identified at boot (agent hot-swap).
- `degraded/` — `CommandRouter` at the HTTP edge: slash commands always,
  free text only while the LLM is down (deferral guard → fast path → fuzzy
  action matcher → "offline" reply). Runs tools via `to_structured_tool`
  (gate + audit apply). Replies are English and never checkpointed.
```

- [ ] **Step 3: Full suite.**

Run: `uv run pytest -q`
Expected: all pass, exactly 1 warning.

- [ ] **Step 4: Commit and open the PR**

```bash
git add README.md CLAUDE.md
git commit -m "docs: degraded mode"
git push -u origin feat/degraded-mode
gh pr create --title "Degraded mode: keep Gosling useful when the LLM is down" --body "Implements docs/superpowers/plans/2026-10-16-degraded-mode.md (spec: Claude Specs/homeassistant-agent/2026-10-16-degraded-mode-design.md)."
```

---

## Self-review notes

- **Spec coverage:**
  - §3 probes, cadence and passive signal → Tasks 2, 3 and 5.
  - Fail-fast clients → Task 1.
  - Boot freeze → Tasks 3 and 5.
  - Fast-path health → Tasks 3, 4 and 9.
  - §4 commands (`/help /actions /run /notes /cancel`, English, skipping lang-mt) → Tasks 8 and 10.
  - §5 fallback (deferral → fast path → matcher → offline) → Tasks 7 and 9.
  - §6 edge, thread hygiene, `/api/health` and frontend → Tasks 5, 10 and 11.
  - §7 errors → Tasks 8, 9 and 10.
  - §8 tests → every task.
  - §9 docs → Task 12.
- **Deviation from the spec:** the matcher filler list drops `start` (the spec was updated to match). `ToolRunner` is its own file (`tools.py`) rather than living inside the router.
