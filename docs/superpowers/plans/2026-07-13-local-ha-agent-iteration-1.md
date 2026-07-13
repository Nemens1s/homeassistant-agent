# Local HA Agent — Iteration 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read-only Home Assistant chat agent (add-on + CLI REPL) with a declarative tool registry, persistent websocket client, skills mechanism, and Ollama/LiteLLM factory, per the spec at `docs/superpowers/specs/2026-07-13-local-ha-agent-design.md`.

**Architecture:** A mylo-style tool registry (`ToolDefinition` + tiers + `ToolResult` envelopes) adapted to LangChain via one adapter that owns validation, error mapping, audit logging, and loop guarding. LangChain v1 `create_agent` runs the loop; a custom middleware trims history and injects current time. REST (httpx) + persistent websocket clients talk to HA.

**Tech Stack:** Python 3.14 (`venv/`), langchain 1.3.x, langchain-ollama, langchain-litellm, langgraph, pydantic v2 + pydantic-settings, httpx, websockets 15, FastAPI, pytest + pytest-asyncio.

## Global Constraints

- Python: always `venv/bin/python`, `venv/bin/pip`, tests via `venv/bin/python -m pytest`.
- langchain v1 API: `from langchain.agents import create_agent`, middleware from `langchain.agents.middleware`. Do NOT use legacy `initialize_agent`/`AgentExecutor`.
- Iteration 1 is read-only: no method on any client writes to HA (no `call_service`, no POST except none at all). `max_tier = 1`.
- Every tool returns `ToolResult(...).to_json()` — compact JSON (`separators=(",", ":")`), never Python `repr`.
- Handlers never raise into the agent loop; the adapter maps exceptions to error envelopes.
- Tool `description` ≤ 400 characters.
- Settings field names are canonical (see Task 2); addon `options.json` keys match them exactly.
- Commit after every task with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Working directory for all commands: `/Users/ilniko/IdeaProjects/homeassistant-ollama-agent`.

---

### Task 1: Baseline commit, dependencies, package skeleton

The repo has a working prototype (flat `app/*.py`) and empty `app/agent/`, `app/ha/`, `app/tools/` dirs. Commit the prototype as a baseline, then clear the ground: the flat modules are removed (the REPL returns as `app/cli.py` in Task 13; the FastAPI app returns in Task 14). The add-on is non-functional between Task 1 and Task 14 — that is expected.

**Files:**
- Create: `requirements-dev.txt`, `pytest.ini`, `app/agent/__init__.py`, `app/ha/__init__.py`, `app/tools/__init__.py`, `app/tools/read/__init__.py`, `app/skills/__init__.py` (empty for now), `tests/__init__.py`
- Modify: `requirements.txt`
- Delete: `app/tools.py`, `app/agent.py`, `app/ha_client.py`, `app/local_agent.py`, `app/main.py`, `app/__pycache__/`

**Interfaces:**
- Produces: importable packages `app.agent`, `app.ha`, `app.tools`, `app.tools.read`, `app.skills`; installed dev+runtime deps; pytest configured with `asyncio_mode = auto`.

- [ ] **Step 1: Commit the existing prototype as a baseline**

```bash
git add -A
git commit -m "chore: baseline prototype scaffold before restructure

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 2: Replace requirements.txt and add requirements-dev.txt**

`requirements.txt`:
```
fastapi
uvicorn[standard]
httpx
langchain>=1.3,<2
langchain-core
langchain-ollama
langchain-litellm
langgraph
pydantic
pydantic-settings
python-dotenv
websockets
pyyaml
```

`requirements-dev.txt`:
```
pytest
pytest-asyncio
```

`pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 3: Remove flat prototype modules, create package skeleton**

```bash
rm app/tools.py app/agent.py app/ha_client.py app/local_agent.py app/main.py
rm -rf app/__pycache__
touch app/agent/__init__.py app/ha/__init__.py app/tools/__init__.py
mkdir -p app/tools/read app/skills tests
touch app/tools/read/__init__.py app/skills/__init__.py tests/__init__.py
```

- [ ] **Step 4: Install and verify imports**

```bash
venv/bin/pip install -r requirements.txt -r requirements-dev.txt
venv/bin/python -c "import app, app.agent, app.ha, app.tools, app.tools.read, app.skills; print('ok')"
venv/bin/python -m pytest --collect-only
```
Expected: `ok`; pytest collects 0 tests without error.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: restructure into packages, add deps and pytest config

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Settings (`app/config.py`)

**Files:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` (pydantic-settings model) with fields:
  `ha_base_url: str`, `ha_token: str`, `llm_provider: str`, `ollama_url: str`, `llm_model: str`, `api_key: str`, `temperature: float`, `seed: int`, `reasoning: bool`, `num_predict: int`, `num_ctx: int`, `keep_alive: int`, `system_prompt: str`, `max_tier: int`, `allowed_domains: list[str]`, `recursion_limit: int`, `max_rows: int`, `ws_connect_timeout: float`; property `ws_url: str`.
- Produces: `load_settings() -> Settings` — reads `/data/options.json` when present (add-on container), else env vars / `.env`.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:
```python
import json

from app.config import Settings, load_settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.ha_base_url == "http://supervisor/core"
    assert s.llm_provider == "ollama"
    assert s.llm_model == "qwen2.5:7b"
    assert s.max_tier == 1
    assert s.temperature == 0.0
    assert s.reasoning is False
    assert s.num_ctx == 8192


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "tok123")
    monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
    s = Settings(_env_file=None)
    assert s.ha_token == "tok123"
    assert s.llm_model == "qwen3:8b"


def test_supervisor_token_alias(monkeypatch):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supertok")
    s = Settings(_env_file=None)
    assert s.ha_token == "supertok"


def test_ws_url_supervisor():
    s = Settings(_env_file=None, ha_base_url="http://supervisor/core")
    assert s.ws_url == "ws://supervisor/core/websocket"


def test_ws_url_direct():
    s = Settings(_env_file=None, ha_base_url="http://192.168.1.10:8123")
    assert s.ws_url == "ws://192.168.1.10:8123/api/websocket"


def test_load_settings_from_options_json(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"llm_model": "llama3.1:8b", "num_ctx": 4096}))
    monkeypatch.setattr("app.config.OPTIONS_FILE", options)
    s = load_settings()
    assert s.llm_model == "llama3.1:8b"
    assert s.num_ctx == 4096
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'` (or ImportError).

- [ ] **Step 3: Implement `app/config.py`**

```python
"""Typed settings — the single place configuration is read.

In the add-on container, options come from /data/options.json (written by the
Supervisor from the add-on's Configuration tab) plus SUPERVISOR_TOKEN from the
environment. In dev, everything comes from .env / environment variables.
"""

import json
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

OPTIONS_FILE = Path("/data/options.json")

DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant for this Home Assistant instance. "
    "Answer questions about the home using the available tools. "
    "Always look up real data with tools instead of guessing. "
    "Be concise and factual."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Home Assistant
    ha_base_url: str = "http://supervisor/core"
    ha_token: str = Field(
        default="", validation_alias=AliasChoices("SUPERVISOR_TOKEN", "HA_TOKEN")
    )
    ws_connect_timeout: float = 10.0

    # LLM backend
    llm_provider: str = "ollama"  # "ollama" or a litellm provider, e.g. "anthropic"
    ollama_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    api_key: str = ""  # cloud provider key, used only by litellm providers

    # Model options — small-model tuning (spec: LLM factory & model settings)
    temperature: float = 0.0
    seed: int = 42
    reasoning: bool = False
    num_predict: int = 2048
    num_ctx: int = 8192
    keep_alive: int = -1

    # Agent behavior
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_tier: int = 1
    allowed_domains: list[str] = ["light", "switch", "automation"]
    recursion_limit: int = 15
    max_rows: int = 50

    @property
    def ws_url(self) -> str:
        base = self.ha_base_url.replace("http://", "ws://").replace("https://", "wss://")
        if base.endswith("/core"):
            return f"{base}/websocket"  # supervisor proxy path
        return f"{base}/api/websocket"  # direct HA instance


def load_settings() -> Settings:
    if OPTIONS_FILE.exists():
        data = json.loads(OPTIONS_FILE.read_text())
        return Settings(**{k: v for k, v in data.items() if v is not None})
    return Settings()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_config.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: typed settings with options.json and env loading

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Tool primitives (`app/tools/base.py`)

**Files:**
- Create: `app/tools/base.py`
- Test: `tests/test_tools_base.py`

**Interfaces:**
- Produces: `Tier` (IntEnum: `READ = 1`, `ACTION = 2`); `ToolResult` with classmethods `ok(data)`, `error(code, message, data=None)` and method `to_json() -> str` (compact); `ToolDefinition` dataclass with fields `name: str`, `description: str`, `params_model: type[BaseModel]`, `tier: Tier`, `handler: Callable[[BaseModel, Any], Awaitable[ToolResult]]`; `bound_rows(rows, *, max_rows, hint=...) -> dict`.

- [ ] **Step 1: Write the failing tests**

`tests/test_tools_base.py`:
```python
import json

from app.tools.base import Tier, ToolResult, bound_rows


def test_tier_values():
    assert Tier.READ == 1
    assert Tier.ACTION == 2


def test_tool_result_ok_json_is_compact():
    out = ToolResult.ok({"a": 1}).to_json()
    assert out == '{"status":"ok","data":{"a":1}}'


def test_tool_result_error_shape():
    out = json.loads(ToolResult.error("entity_not_found", "no such entity").to_json())
    assert out["status"] == "error"
    assert out["error"] == {"code": "entity_not_found", "message": "no such entity"}


def test_tool_result_error_with_data():
    out = json.loads(
        ToolResult.error("entity_not_found", "nope", data={"did_you_mean": ["light.kitchen"]}).to_json()
    )
    assert out["data"] == {"did_you_mean": ["light.kitchen"]}


def test_bound_rows_under_limit():
    env = bound_rows([1, 2], max_rows=5)
    assert env == {"rows": [1, 2], "total": 2}


def test_bound_rows_truncates_and_reports_total():
    env = bound_rows(list(range(10)), max_rows=3, hint="narrow it")
    assert env["rows"] == [0, 1, 2]
    assert env["total"] == 10
    assert env["truncated"] is True
    assert env["hint"] == "narrow it"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_tools_base.py -v`
Expected: FAIL — `ImportError` (module missing).

- [ ] **Step 3: Implement `app/tools/base.py`**

```python
"""Core tool abstractions: tiers, result envelope, declarative definition.

Every tool is a ToolDefinition: a pydantic params model (validation + JSON
schema for the LLM from one source) and an async handler returning a
ToolResult. Handlers never see unvalidated input and never raise into the
agent loop — the adapter (tools/adapter.py) enforces both.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class Tier(enum.IntEnum):
    READ = 1
    ACTION = 2


@dataclass(slots=True)
class ToolResult:
    status: str  # "ok" | "error"
    data: Any = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def ok(cls, data: Any) -> "ToolResult":
        return cls(status="ok", data=data)

    @classmethod
    def error(cls, code: str, message: str, data: Any = None) -> "ToolResult":
        return cls(status="error", data=data, error_code=code, error_message=message)

    def to_json(self) -> str:
        out: dict[str, Any] = {"status": self.status}
        if self.error_code is not None:
            out["error"] = {"code": self.error_code, "message": self.error_message or ""}
        if self.data is not None:
            out["data"] = self.data
        return json.dumps(out, separators=(",", ":"), default=str)


@dataclass(slots=True)
class ToolDefinition:
    """One tool. name: snake_case id the LLM calls. description: what the
    LLM sees (keep under ~400 chars). handler: async (params, ctx) -> ToolResult."""

    name: str
    description: str
    params_model: type[BaseModel]
    tier: Tier
    handler: Callable[[BaseModel, Any], Awaitable[ToolResult]]


def bound_rows(
    rows: list, *, max_rows: int, hint: str = "Narrow with filters to see the rest."
) -> dict[str, Any]:
    """Cap a result list while always reporting the true total, so the model
    knows results were cut and to narrow — instead of silently missing data."""
    shown = rows[:max_rows]
    env: dict[str, Any] = {"rows": shown, "total": len(rows)}
    if len(rows) > max_rows:
        env["truncated"] = True
        env["hint"] = hint
    return env
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_tools_base.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/tools/base.py tests/test_tools_base.py
git commit -m "feat: tool primitives — Tier, ToolResult, ToolDefinition, bound_rows

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Tool registry (`app/tools/registry.py`)

**Files:**
- Create: `app/tools/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `register(tool: ToolDefinition) -> ToolDefinition` (raises `ValueError` on duplicate name); `get(name) -> ToolDefinition | None`; `tools_for_tier(max_tier: int) -> list[ToolDefinition]` (sorted by name); `load_all(modules=_DEFAULT_MODULES) -> None`; `_reset_for_tests() -> None`; `_DEFAULT_MODULES: tuple[str, ...]` listing the eight `app.tools.read.*` modules (added in Tasks 8–11).

- [ ] **Step 1: Write the failing tests**

`tests/test_registry.py`:
```python
import pytest
from pydantic import BaseModel

from app.tools import registry
from app.tools.base import Tier, ToolDefinition, ToolResult


class _Params(BaseModel):
    x: int = 0


async def _handler(params, ctx):
    return ToolResult.ok(params.x)


def _defn(name, tier=Tier.READ):
    return ToolDefinition(
        name=name, description="d", params_model=_Params, tier=tier, handler=_handler
    )


@pytest.fixture(autouse=True)
def clean_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


def test_register_and_get():
    d = _defn("alpha")
    registry.register(d)
    assert registry.get("alpha") is d
    assert registry.get("missing") is None


def test_duplicate_name_rejected():
    registry.register(_defn("alpha"))
    with pytest.raises(ValueError):
        registry.register(_defn("alpha"))


def test_tools_for_tier_filters_and_sorts():
    registry.register(_defn("zeta", Tier.READ))
    registry.register(_defn("beta", Tier.ACTION))
    registry.register(_defn("alpha", Tier.READ))
    read_only = registry.tools_for_tier(1)
    assert [t.name for t in read_only] == ["alpha", "zeta"]
    everything = registry.tools_for_tier(2)
    assert [t.name for t in everything] == ["alpha", "beta", "zeta"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_registry.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `app/tools/registry.py`**

```python
"""Tool catalogue. Tool modules call register() at import time; load_all()
imports them. Adding a tool = one new module + one line in _DEFAULT_MODULES.

tools_for_tier() is the read-only enforcement point: with max_tier=1 the
agent is never handed anything above Tier.READ — the tool does not exist
for the model.
"""

from __future__ import annotations

import importlib
import sys

from app.tools.base import ToolDefinition

_REGISTRY: dict[str, ToolDefinition] = {}

_DEFAULT_MODULES: tuple[str, ...] = (
    "app.tools.read.get_entity_state",
    "app.tools.read.list_entities",
    "app.tools.read.get_history",
    "app.tools.read.get_logbook",
    "app.tools.read.get_error_log",
    "app.tools.read.get_areas_and_devices",
    "app.tools.read.get_automations",
    "app.tools.read.load_skill",
)


def register(tool: ToolDefinition) -> ToolDefinition:
    if tool.name in _REGISTRY:
        raise ValueError(f"tool already registered: {tool.name!r}")
    _REGISTRY[tool.name] = tool
    return tool


def get(name: str) -> ToolDefinition | None:
    return _REGISTRY.get(name)


def tools_for_tier(max_tier: int) -> list[ToolDefinition]:
    return sorted(
        (t for t in _REGISTRY.values() if t.tier <= max_tier), key=lambda t: t.name
    )


def load_all(modules: tuple[str, ...] = _DEFAULT_MODULES) -> None:
    """Import every tool module so its register() call runs. Reload if already
    imported, so tests that reset the registry can re-trigger registration."""
    for mod in modules:
        existing = sys.modules.get(mod)
        if existing is not None:
            importlib.reload(existing)
        else:
            importlib.import_module(mod)


def _reset_for_tests() -> None:
    _REGISTRY.clear()
```

Note: `load_all()` will fail until Tasks 8–11 create all eight modules — that's fine; nothing calls it with defaults until Task 13.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_registry.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/tools/registry.py tests/test_registry.py
git commit -m "feat: tool registry with tier filtering

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: REST client (`app/ha/rest.py`)

**Files:**
- Create: `app/ha/rest.py`
- Test: `tests/test_rest.py`

**Interfaces:**
- Produces: `RestClient(base_url: str, token: str, timeout: float = 30.0, transport: httpx.BaseTransport | None = None)` with async methods `ping() -> bool`, `get_state(entity_id) -> dict`, `list_states() -> list[dict]`, `get_history(entity_id, start_time, end_time=None) -> list`, `get_logbook(start_time, end_time=None) -> list`, `get_error_log() -> str`, `get_automation_config(automation_id) -> dict`, `aclose()`. GET-only — no write method exists in iteration 1. Raises `httpx.HTTPStatusError` on non-2xx (the adapter maps these).

- [ ] **Step 1: Write the failing tests**

`tests/test_rest.py`:
```python
import httpx
import pytest

from app.ha.rest import RestClient


def _client(handler):
    return RestClient("http://ha.test", "tok", transport=httpx.MockTransport(handler))


async def test_get_state_sends_auth_and_parses():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.url.path == "/api/states/light.kitchen"
        return httpx.Response(200, json={"entity_id": "light.kitchen", "state": "on"})

    client = _client(handler)
    data = await client.get_state("light.kitchen")
    assert data["state"] == "on"
    await client.aclose()


async def test_get_state_raises_on_404():
    client = _client(lambda req: httpx.Response(404, json={"message": "not found"}))
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_state("light.nope")
    await client.aclose()


async def test_get_history_params():
    def handler(request):
        assert request.url.path == "/api/history/period/2026-07-12T00:00:00"
        assert request.url.params["filter_entity_id"] == "sensor.temp"
        assert request.url.params["end_time"] == "2026-07-13T00:00:00"
        return httpx.Response(200, json=[[{"state": "21.5"}]])

    client = _client(handler)
    data = await client.get_history("sensor.temp", "2026-07-12T00:00:00", "2026-07-13T00:00:00")
    assert data == [[{"state": "21.5"}]]
    await client.aclose()


async def test_ping():
    client = _client(lambda req: httpx.Response(200, json={"message": "API running."}))
    assert await client.ping() is True
    await client.aclose()


async def test_error_log_returns_text():
    client = _client(lambda req: httpx.Response(200, text="line1\nline2"))
    assert await client.get_error_log() == "line1\nline2"
    await client.aclose()


def test_no_write_methods_exist():
    banned = ("post", "call_service", "set_state", "turn_on", "turn_off")
    for name in banned:
        assert not hasattr(RestClient, name)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_rest.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `app/ha/rest.py`**

```python
"""GET-only client for the Home Assistant REST API.

Iteration 1 is read-only by construction: there is deliberately no
call_service / POST method in this class. Iteration 2 adds exactly one,
guarded by the domain allowlist.
"""

from __future__ import annotations

import httpx


class RestClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str, params: dict | None = None):
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def ping(self) -> bool:
        resp = await self._client.get("/api/")
        resp.raise_for_status()
        return True

    async def get_state(self, entity_id: str) -> dict:
        return await self._get_json(f"/api/states/{entity_id}")

    async def list_states(self) -> list[dict]:
        return await self._get_json("/api/states")

    async def get_history(
        self, entity_id: str, start_time: str, end_time: str | None = None
    ) -> list:
        params: dict = {"filter_entity_id": entity_id}
        if end_time:
            params["end_time"] = end_time
        return await self._get_json(f"/api/history/period/{start_time}", params)

    async def get_logbook(self, start_time: str, end_time: str | None = None) -> list:
        params: dict = {}
        if end_time:
            params["end_time"] = end_time
        return await self._get_json(f"/api/logbook/{start_time}", params)

    async def get_error_log(self) -> str:
        resp = await self._client.get("/api/error_log")
        resp.raise_for_status()
        return resp.text

    async def get_automation_config(self, automation_id: str) -> dict:
        return await self._get_json(f"/api/config/automation/config/{automation_id}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_rest.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/ha/rest.py tests/test_rest.py
git commit -m "feat: GET-only Home Assistant REST client

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Websocket client (`app/ha/websocket.py`)

**Files:**
- Create: `app/ha/websocket.py`
- Test: `tests/test_websocket.py`

**Interfaces:**
- Consumes: nothing internal (stdlib + `websockets`).
- Produces: `WebSocketClient(url: str, token: str, request_timeout: float = 10.0)` with `async start(connect_timeout: float = 10.0)` (connect + auth + background reader; raises `TimeoutError` if not connected in time), `async stop()`, `async request(msg_type: str, **payload) -> Any` (returns HA's `result` field; raises `RuntimeError` on HA error response, `TimeoutError` on timeout), `async request_cached(msg_type: str, ttl: float = 60.0) -> Any`, property `connected: bool`. Reconnects with exponential backoff (1 s → 30 s cap) if the connection drops.

- [ ] **Step 1: Write the failing tests**

`tests/test_websocket.py`:
```python
import asyncio
import json

import pytest
import websockets

from app.ha.websocket import WebSocketClient

AREAS = [{"area_id": "living", "name": "Living room"}]


async def _fake_ha(ws):
    await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.7"}))
    msg = json.loads(await ws.recv())
    if msg.get("access_token") != "secret":
        await ws.send(json.dumps({"type": "auth_invalid", "message": "bad token"}))
        return
    await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.7"}))
    async for raw in ws:
        msg = json.loads(raw)
        if msg["type"] == "config/area_registry/list":
            await ws.send(
                json.dumps(
                    {"id": msg["id"], "type": "result", "success": True, "result": AREAS}
                )
            )
        else:
            await ws.send(
                json.dumps(
                    {
                        "id": msg["id"],
                        "type": "result",
                        "success": False,
                        "error": {"code": "unknown_command", "message": "unknown"},
                    }
                )
            )


@pytest.fixture
async def server_url():
    async with websockets.serve(_fake_ha, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def test_auth_and_request(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    assert client.connected
    result = await client.request("config/area_registry/list")
    assert result == AREAS
    await client.stop()


async def test_bad_token_fails_start(server_url):
    client = WebSocketClient(server_url, "wrong")
    with pytest.raises(TimeoutError):
        await client.start(connect_timeout=1)
    await client.stop()


async def test_error_response_raises(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    with pytest.raises(RuntimeError):
        await client.request("no/such/command")
    await client.stop()


async def test_request_cached_hits_cache(server_url):
    client = WebSocketClient(server_url, "secret")
    await client.start(connect_timeout=5)
    a = await client.request_cached("config/area_registry/list")
    b = await client.request_cached("config/area_registry/list")
    assert a == b == AREAS
    # ids increment only once: second call served from cache
    assert client._next_id == 2
    await client.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_websocket.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `app/ha/websocket.py`**

```python
"""Persistent Home Assistant websocket client.

One connection for the process lifetime: auth handshake, id-correlated
request/response via futures, reconnect with backoff. Used for the
websocket-only APIs (area/device/entity registries). Event subscriptions
are out of scope but nothing here precludes them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets

log = logging.getLogger("agent.ws")


class WebSocketClient:
    def __init__(self, url: str, token: str, request_timeout: float = 10.0):
        self._url = url
        self._token = token
        self._timeout = request_timeout
        self._conn: Any = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._cache: dict[str, tuple[float, Any]] = {}
        self._runner: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._closing = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def start(self, connect_timeout: float = 10.0) -> None:
        self._closing = False
        self._runner = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=connect_timeout)
        except TimeoutError:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._closing = True
        if self._runner is not None:
            self._runner.cancel()
            self._runner = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self._connected.clear()
        self._fail_pending(ConnectionError("websocket client stopped"))

    async def request(self, msg_type: str, **payload: Any) -> Any:
        await asyncio.wait_for(self._connected.wait(), timeout=self._timeout)
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._conn.send(json.dumps({"id": msg_id, "type": msg_type, **payload}))
        return await asyncio.wait_for(fut, timeout=self._timeout)

    async def request_cached(self, msg_type: str, ttl: float = 60.0) -> Any:
        hit = self._cache.get(msg_type)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]
        result = await self.request(msg_type)
        self._cache[msg_type] = (time.monotonic(), result)
        return result

    async def _run(self) -> None:
        backoff = 1
        while not self._closing:
            try:
                async with websockets.connect(self._url) as conn:
                    await self._auth(conn)
                    self._conn = conn
                    self._connected.set()
                    backoff = 1
                    log.info("websocket connected: %s", self._url)
                    async for raw in conn:
                        self._dispatch(json.loads(raw))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("websocket dropped: %s — reconnecting in %ss", exc, backoff)
            self._connected.clear()
            self._conn = None
            self._fail_pending(ConnectionError("websocket disconnected"))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _auth(self, conn: Any) -> None:
        msg = json.loads(await conn.recv())
        if msg.get("type") != "auth_required":
            raise ConnectionError(f"unexpected first message: {msg.get('type')}")
        await conn.send(json.dumps({"type": "auth", "access_token": self._token}))
        msg = json.loads(await conn.recv())
        if msg.get("type") != "auth_ok":
            raise ConnectionError(f"websocket auth failed: {msg.get('message', '')}")

    def _dispatch(self, msg: dict) -> None:
        fut = self._pending.pop(msg.get("id", -1), None)
        if fut is None or fut.done():
            return
        if msg.get("success"):
            fut.set_result(msg.get("result"))
        else:
            error = msg.get("error") or {}
            fut.set_exception(RuntimeError(error.get("message", "websocket command failed")))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_websocket.py -v`
Expected: 4 passed. (The bad-token test logs reconnect warnings — that's the backoff loop working; `stop()` ends it.)

- [ ] **Step 5: Commit**

```bash
git add app/ha/websocket.py tests/test_websocket.py
git commit -m "feat: persistent HA websocket client with auth, correlation, reconnect

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: ToolContext + LangChain adapter (`app/tools/context.py`, `app/tools/adapter.py`)

**Files:**
- Create: `app/tools/context.py`, `app/tools/adapter.py`
- Test: `tests/test_adapter.py`

**Interfaces:**
- Consumes: `Settings` (Task 2), `ToolDefinition`/`ToolResult`/`Tier` (Task 3), `registry.tools_for_tier` (Task 4), `RestClient` (Task 5), `WebSocketClient` (Task 6).
- Produces: `ToolContext` dataclass (`settings: Settings`, `rest: RestClient`, `ws: WebSocketClient | None = None`, `skills_dir: Path = <app/skills>`); `LoopGuard` with `is_repeat(name, args_json) -> bool`; `to_structured_tool(defn, ctx, guard) -> StructuredTool`; `build_tools(ctx, max_tier) -> list[StructuredTool]`. Audit logger name: `"agent.tools"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_adapter.py`:
```python
import json

import httpx
import pytest
from pydantic import BaseModel, Field

from app.config import Settings
from app.tools.adapter import LoopGuard, build_tools, to_structured_tool
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools import registry
from app.tools.context import ToolContext


class _Params(BaseModel):
    entity_id: str = Field(description="entity id")


class _FakeRest:
    async def list_states(self):
        return [{"entity_id": "light.kitchen"}, {"entity_id": "light.bedroom"}]


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=_FakeRest(), ws=None)


def _make_tool(handler, name="demo"):
    defn = ToolDefinition(
        name=name, description="demo tool", params_model=_Params,
        tier=Tier.READ, handler=handler,
    )
    return to_structured_tool(defn, _ctx(), LoopGuard())


async def test_ok_result_passthrough():
    async def handler(params, ctx):
        return ToolResult.ok({"state": "on"})

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out == {"status": "ok", "data": {"state": "on"}}


async def test_404_maps_to_entity_not_found_with_suggestions():
    async def handler(params, ctx):
        raise httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(404),
        )

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitche"}))
    assert out["status"] == "error"
    assert out["error"]["code"] == "entity_not_found"
    assert "light.kitchen" in out["data"]["did_you_mean"]


async def test_connect_error_maps_to_ha_unreachable():
    async def handler(params, ctx):
        raise httpx.ConnectError("refused")

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out["error"]["code"] == "ha_unreachable"


async def test_timeout_maps_to_ha_timeout():
    async def handler(params, ctx):
        raise TimeoutError()

    tool = _make_tool(handler)
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out["error"]["code"] == "ha_timeout"


async def test_loop_guard_blocks_identical_consecutive_call():
    async def handler(params, ctx):
        return ToolResult.ok("fine")

    tool = _make_tool(handler)
    first = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    second = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert first["status"] == "ok"
    assert second["error"]["code"] == "repeated_call"
    third = json.loads(await tool.ainvoke({"entity_id": "light.bedroom"}))
    assert third["status"] == "ok"


async def test_audit_log_line(caplog):
    async def handler(params, ctx):
        return ToolResult.ok("x")

    tool = _make_tool(handler)
    with caplog.at_level("INFO", logger="agent.tools"):
        await tool.ainvoke({"entity_id": "light.kitchen"})
    line = caplog.records[-1].getMessage()
    assert "tool=demo" in line and "status=ok" in line and "duration_ms=" in line


def test_build_tools_filters_by_tier():
    registry._reset_for_tests()

    async def handler(params, ctx):
        return ToolResult.ok(None)

    registry.register(ToolDefinition("read_one", "d", _Params, Tier.READ, handler))
    registry.register(ToolDefinition("act_one", "d", _Params, Tier.ACTION, handler))
    tools = build_tools(_ctx(), max_tier=1)
    assert [t.name for t in tools] == ["read_one"]
    registry._reset_for_tests()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_adapter.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `app/tools/context.py`**

```python
"""Dependencies handed to every tool handler. Handlers never import clients
directly — tests pass a fake context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings

_DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@dataclass
class ToolContext:
    settings: Settings
    rest: Any  # RestClient; typed loosely so tests can pass fakes
    ws: Any = None  # WebSocketClient | None
    skills_dir: Path = field(default=_DEFAULT_SKILLS_DIR)
```

- [ ] **Step 4: Implement `app/tools/adapter.py`**

```python
"""ToolDefinition → LangChain StructuredTool. The single seam where all
cross-cutting behavior lives: validation envelopes, exception → error-code
mapping, per-call audit logging, and the repeated-call loop guard. Handlers
stay pure; nothing below this layer raises into the agent loop.
"""

from __future__ import annotations

import difflib
import json
import logging
import time

import httpx
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from app.tools import registry
from app.tools.base import ToolDefinition, ToolResult
from app.tools.context import ToolContext

log = logging.getLogger("agent.tools")


class LoopGuard:
    """Shared across one agent's tools: flags an identical consecutive call."""

    def __init__(self) -> None:
        self._last: tuple[str, str] | None = None

    def is_repeat(self, name: str, args_json: str) -> bool:
        key = (name, args_json)
        if key == self._last:
            return True
        self._last = key
        return False


def _validation_message(exc: ValidationError) -> str:
    problems = [
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
    ]
    return "Invalid parameters — " + "; ".join(problems)


async def _did_you_mean(ctx: ToolContext, entity_id: str) -> list[str]:
    try:
        states = await ctx.rest.list_states()
        ids = [s["entity_id"] for s in states]
        return difflib.get_close_matches(entity_id, ids, n=3, cutoff=0.5)
    except Exception:  # suggestion is best-effort; never mask the real error
        return []


def to_structured_tool(
    defn: ToolDefinition, ctx: ToolContext, guard: LoopGuard
) -> StructuredTool:
    async def _run(**kwargs) -> str:
        args_json = json.dumps(kwargs, sort_keys=True, default=str)
        if guard.is_repeat(defn.name, args_json):
            result = ToolResult.error(
                "repeated_call",
                "You already called this tool with identical arguments. "
                "Use the previous result or try a different approach.",
            )
            log.info("tool=%s status=error duration_ms=0 args=%s", defn.name, args_json)
            return result.to_json()

        started = time.monotonic()
        try:
            params = defn.params_model(**kwargs)
            result = await defn.handler(params, ctx)
        except ValidationError as exc:
            result = ToolResult.error("invalid_params", _validation_message(exc))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                entity_id = str(kwargs.get("entity_id", ""))
                data = (
                    {"did_you_mean": await _did_you_mean(ctx, entity_id)}
                    if entity_id
                    else None
                )
                result = ToolResult.error(
                    "entity_not_found", f"No entity {entity_id!r}.", data=data
                )
            else:
                result = ToolResult.error(
                    "ha_unreachable",
                    f"Home Assistant returned HTTP {exc.response.status_code}.",
                )
        except (httpx.HTTPError, ConnectionError) as exc:
            result = ToolResult.error(
                "ha_unreachable", f"Could not reach Home Assistant: {exc}."
            )
        except TimeoutError:
            result = ToolResult.error(
                "ha_timeout", "Home Assistant did not answer in time."
            )
        duration_ms = round((time.monotonic() - started) * 1000)
        log.info(
            "tool=%s status=%s duration_ms=%s args=%s",
            defn.name, result.status, duration_ms, args_json,
        )
        return result.to_json()

    return StructuredTool(
        name=defn.name,
        description=defn.description,
        args_schema=defn.params_model,
        coroutine=_run,
        handle_validation_error=lambda exc: ToolResult.error(
            "invalid_params", _validation_message(exc)
        ).to_json(),
    )


def build_tools(ctx: ToolContext, max_tier: int) -> list[StructuredTool]:
    guard = LoopGuard()
    return [to_structured_tool(d, ctx, guard) for d in registry.tools_for_tier(max_tier)]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_adapter.py -v`
Expected: 8 passed. If `test_404_maps_to_entity_not_found_with_suggestions` fails because LangChain's own validation intercepts first, the handler is never reached — that test raises from inside the handler, so it will pass; validation interception is covered by `handle_validation_error`.

- [ ] **Step 6: Commit**

```bash
git add app/tools/context.py app/tools/adapter.py tests/test_adapter.py
git commit -m "feat: ToolContext and LangChain adapter with error mapping, audit log, loop guard

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Read tools — entity state & listing

**Files:**
- Create: `app/tools/read/get_entity_state.py`, `app/tools/read/list_entities.py`
- Test: `tests/test_read_entities.py`

**Interfaces:**
- Consumes: `ToolContext` (`ctx.rest.get_state`, `ctx.rest.list_states`, `ctx.ws.request_cached`, `ctx.settings.max_rows`), `register`, `ToolDefinition`, `Tier`, `ToolResult`, `bound_rows`.
- Produces: registered tools `get_entity_state(entity_id)` and `list_entities(domain="", area="")`.

- [ ] **Step 1: Write the failing tests**

`tests/test_read_entities.py`:
```python
import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext

STATES = [
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen Light", "brightness": 200},
     "last_changed": "2026-07-13T10:00:00+00:00"},
    {"entity_id": "light.bedroom", "state": "off",
     "attributes": {"friendly_name": "Bedroom Light"},
     "last_changed": "2026-07-13T09:00:00+00:00"},
    {"entity_id": "sensor.temp", "state": "21.5",
     "attributes": {"friendly_name": "Temperature"},
     "last_changed": "2026-07-13T10:30:00+00:00"},
]


class FakeRest:
    async def get_state(self, entity_id):
        for s in STATES:
            if s["entity_id"] == entity_id:
                return s
        raise AssertionError("test should not request unknown ids directly")

    async def list_states(self):
        return STATES


class FakeWS:
    def __init__(self):
        self.registries = {
            "config/area_registry/list": [{"area_id": "kitchen", "name": "Kitchen"}],
            "config/device_registry/list": [{"id": "dev1", "area_id": "kitchen"}],
            "config/entity_registry/list": [
                {"entity_id": "light.kitchen", "area_id": None, "device_id": "dev1"},
                {"entity_id": "light.bedroom", "area_id": None, "device_id": None},
            ],
        }

    async def request_cached(self, msg_type, ttl=60.0):
        return self.registries[msg_type]


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(
        ("app.tools.read.get_entity_state", "app.tools.read.list_entities")
    )
    yield
    registry._reset_for_tests()


def _ctx(ws=None):
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=ws)


async def test_get_entity_state():
    defn = registry.get("get_entity_state")
    result = await defn.handler(defn.params_model(entity_id="light.kitchen"), _ctx())
    assert result.status == "ok"
    assert result.data["state"] == "on"
    assert result.data["attributes"]["brightness"] == 200


async def test_list_entities_domain_filter():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(domain="light"), _ctx())
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen", "light.bedroom"]
    assert result.data["total"] == 2


async def test_list_entities_area_filter():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(area="Kitchen"), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    ids = [r["entity_id"] for r in result.data["rows"]]
    assert ids == ["light.kitchen"]  # via device dev1 in area kitchen


async def test_list_entities_unknown_area():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(area="Garage"), _ctx(ws=FakeWS()))
    assert result.status == "error"
    assert result.error_code == "area_not_found"
    assert result.data["available_areas"] == ["Kitchen"]


async def test_list_entities_area_without_ws():
    defn = registry.get("list_entities")
    result = await defn.handler(defn.params_model(area="Kitchen"), _ctx(ws=None))
    assert result.status == "error"
    assert result.error_code == "ws_unavailable"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_read_entities.py -v`
Expected: FAIL — `ModuleNotFoundError` from `load_all`.

- [ ] **Step 3: Implement `app/tools/read/get_entity_state.py`**

```python
from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(
        description="Full entity id, e.g. 'light.kitchen' or 'sensor.living_room_temperature'"
    )


async def handler(params: Params, ctx) -> ToolResult:
    state = await ctx.rest.get_state(params.entity_id)
    return ToolResult.ok(
        {
            "entity_id": state["entity_id"],
            "state": state["state"],
            "attributes": state.get("attributes", {}),
            "last_changed": state.get("last_changed"),
        }
    )


register(
    ToolDefinition(
        name="get_entity_state",
        description="Get the current state and attributes of one Home Assistant entity by its full entity_id.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
```

- [ ] **Step 4: Implement `app/tools/read/list_entities.py`**

```python
from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    domain: str = Field(
        default="", description="Optional domain filter, e.g. 'light', 'sensor', 'automation'."
    )
    area: str = Field(
        default="", description="Optional area name filter, e.g. 'Living room'."
    )


async def _entity_ids_in_area(ctx, area_name: str) -> set[str] | None:
    areas = await ctx.ws.request_cached("config/area_registry/list")
    match = next((a for a in areas if a["name"].lower() == area_name.lower()), None)
    if match is None:
        return None
    area_id = match["area_id"]
    devices = await ctx.ws.request_cached("config/device_registry/list")
    device_ids = {d["id"] for d in devices if d.get("area_id") == area_id}
    entities = await ctx.ws.request_cached("config/entity_registry/list")
    ids: set[str] = set()
    for e in entities:
        # entity's own area assignment overrides its device's area
        if e.get("area_id") == area_id or (
            e.get("area_id") is None and e.get("device_id") in device_ids
        ):
            ids.add(e["entity_id"])
    return ids


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    if params.domain:
        states = [s for s in states if s["entity_id"].startswith(params.domain + ".")]
    if params.area:
        if ctx.ws is None:
            return ToolResult.error(
                "ws_unavailable",
                "Area filtering needs the websocket connection, which is not available. Filter by domain instead.",
            )
        entity_ids = await _entity_ids_in_area(ctx, params.area)
        if entity_ids is None:
            areas = await ctx.ws.request_cached("config/area_registry/list")
            return ToolResult.error(
                "area_not_found",
                f"No area named {params.area!r}.",
                data={"available_areas": [a["name"] for a in areas]},
            )
        states = [s for s in states if s["entity_id"] in entity_ids]
    rows = [
        {
            "entity_id": s["entity_id"],
            "state": s["state"],
            "name": s.get("attributes", {}).get("friendly_name", ""),
        }
        for s in states
    ]
    return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))


register(
    ToolDefinition(
        name="list_entities",
        description="List entities with current state and friendly name. Filter by domain (e.g. 'light') and/or area name (e.g. 'Living room'). Unfiltered lists are truncated.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_read_entities.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add app/tools/read/get_entity_state.py app/tools/read/list_entities.py tests/test_read_entities.py
git commit -m "feat: get_entity_state and list_entities read tools

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Read tools — history, logbook, error log

**Files:**
- Create: `app/tools/read/get_history.py`, `app/tools/read/get_logbook.py`, `app/tools/read/get_error_log.py`
- Test: `tests/test_read_diagnostics.py`

**Interfaces:**
- Consumes: `ctx.rest.get_history/get_logbook/get_error_log`, `bound_rows`, `ctx.settings.max_rows`.
- Produces: registered tools `get_history(entity_id, start_time, end_time="")`, `get_logbook(start_time, end_time="")`, `get_error_log()` (no params).

- [ ] **Step 1: Write the failing tests**

`tests/test_read_diagnostics.py`:
```python
import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    async def get_history(self, entity_id, start_time, end_time=None):
        return [[
            {"state": "off", "last_changed": "2026-07-12T20:00:00+00:00"},
            {"state": "on", "last_changed": "2026-07-12T21:00:00+00:00"},
        ]]

    async def get_logbook(self, start_time, end_time=None):
        return [
            {"when": "2026-07-12T21:00:00+00:00", "name": "Kitchen Light",
             "message": "turned on", "entity_id": "light.kitchen"},
        ]

    async def get_error_log(self):
        return "\n".join(f"line {i}" for i in range(1, 101))


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all((
        "app.tools.read.get_history",
        "app.tools.read.get_logbook",
        "app.tools.read.get_error_log",
    ))
    yield
    registry._reset_for_tests()


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=None)


async def test_get_history_compacts_rows():
    defn = registry.get("get_history")
    result = await defn.handler(
        defn.params_model(entity_id="light.kitchen", start_time="2026-07-12T00:00:00"),
        _ctx(),
    )
    assert result.status == "ok"
    assert result.data["rows"] == [
        {"state": "off", "at": "2026-07-12T20:00:00+00:00"},
        {"state": "on", "at": "2026-07-12T21:00:00+00:00"},
    ]


async def test_get_logbook_rows():
    defn = registry.get("get_logbook")
    result = await defn.handler(
        defn.params_model(start_time="2026-07-12T00:00:00"), _ctx()
    )
    assert result.status == "ok"
    row = result.data["rows"][0]
    assert row == {
        "at": "2026-07-12T21:00:00+00:00", "name": "Kitchen Light",
        "message": "turned on", "entity_id": "light.kitchen",
    }


async def test_get_error_log_tails_50_lines():
    defn = registry.get("get_error_log")
    result = await defn.handler(defn.params_model(), _ctx())
    assert result.status == "ok"
    assert len(result.data["lines"]) == 50
    assert result.data["lines"][-1] == "line 100"
    assert result.data["total_lines"] == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_read_diagnostics.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the three tool modules**

`app/tools/read/get_history.py`:
```python
from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(description="Full entity id, e.g. 'sensor.living_room_temperature'")
    start_time: str = Field(description="ISO8601 start, e.g. '2026-07-12T00:00:00'")
    end_time: str = Field(default="", description="ISO8601 end; empty means now")


async def handler(params: Params, ctx) -> ToolResult:
    data = await ctx.rest.get_history(
        params.entity_id, params.start_time, params.end_time or None
    )
    changes = data[0] if data else []
    rows = [{"state": c.get("state"), "at": c.get("last_changed", "")} for c in changes]
    return ToolResult.ok(
        bound_rows(rows, max_rows=ctx.settings.max_rows,
                   hint="Narrow the time range to see the rest.")
    )


register(
    ToolDefinition(
        name="get_history",
        description="Get historical state changes for one entity between two ISO8601 timestamps. Use the current time from the system prompt to compute ranges like 'yesterday'.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
```

`app/tools/read/get_logbook.py`:
```python
from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    start_time: str = Field(description="ISO8601 start, e.g. '2026-07-12T00:00:00'")
    end_time: str = Field(default="", description="ISO8601 end; empty means now")


async def handler(params: Params, ctx) -> ToolResult:
    entries = await ctx.rest.get_logbook(params.start_time, params.end_time or None)
    rows = [
        {
            "at": e.get("when", ""),
            "name": e.get("name", ""),
            "message": e.get("message", ""),
            "entity_id": e.get("entity_id", ""),
        }
        for e in entries
    ]
    return ToolResult.ok(
        bound_rows(rows, max_rows=ctx.settings.max_rows,
                   hint="Narrow the time range to see the rest.")
    )


register(
    ToolDefinition(
        name="get_logbook",
        description="Get logbook entries (state changes, triggered automations, events) between two ISO8601 timestamps.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
```

`app/tools/read/get_error_log.py`:
```python
from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register

TAIL_LINES = 50


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    text = await ctx.rest.get_error_log()
    lines = text.strip().splitlines()
    return ToolResult.ok({"lines": lines[-TAIL_LINES:], "total_lines": len(lines)})


register(
    ToolDefinition(
        name="get_error_log",
        description="Get the last lines of Home Assistant's error log. Useful for 'why is X broken' questions.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_read_diagnostics.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add app/tools/read/get_history.py app/tools/read/get_logbook.py app/tools/read/get_error_log.py tests/test_read_diagnostics.py
git commit -m "feat: history, logbook, and error log read tools

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Read tools — areas/devices topology & automations

**Files:**
- Create: `app/tools/read/get_areas_and_devices.py`, `app/tools/read/get_automations.py`
- Test: `tests/test_read_topology.py`

**Interfaces:**
- Consumes: `ctx.ws.request_cached` (three registries), `ctx.rest.list_states`, `ctx.rest.get_automation_config`.
- Produces: registered tools `get_areas_and_devices()` and `get_automations(entity_id="")`.

- [ ] **Step 1: Write the failing tests**

`tests/test_read_topology.py`:
```python
import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeWS:
    def __init__(self):
        self.registries = {
            "config/area_registry/list": [
                {"area_id": "kitchen", "name": "Kitchen"},
                {"area_id": "bedroom", "name": "Bedroom"},
            ],
            "config/device_registry/list": [
                {"id": "dev1", "area_id": "kitchen", "name_by_user": None, "name": "Hue Bulb"},
                {"id": "dev2", "area_id": None, "name_by_user": "Odd Sensor", "name": "Sensor X"},
            ],
            "config/entity_registry/list": [
                {"entity_id": "light.kitchen", "area_id": None, "device_id": "dev1"},
                {"entity_id": "sensor.odd", "area_id": None, "device_id": "dev2"},
                {"entity_id": "light.bedroom_lamp", "area_id": "bedroom", "device_id": None},
            ],
        }

    async def request_cached(self, msg_type, ttl=60.0):
        return self.registries[msg_type]


class FakeRest:
    def __init__(self):
        self.states = [
            {"entity_id": "automation.night_lights", "state": "on",
             "attributes": {"friendly_name": "Night lights", "id": "1234",
                            "last_triggered": "2026-07-12T22:00:00+00:00"}},
            {"entity_id": "automation.yaml_one", "state": "off",
             "attributes": {"friendly_name": "Yaml one"}},
        ]

    async def list_states(self):
        return self.states

    async def get_automation_config(self, automation_id):
        assert automation_id == "1234"
        return {"alias": "Night lights", "trigger": [{"platform": "sun"}], "action": []}


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all((
        "app.tools.read.get_areas_and_devices",
        "app.tools.read.get_automations",
    ))
    yield
    registry._reset_for_tests()


def _ctx(ws=None):
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=ws)


async def test_topology_groups_by_area():
    defn = registry.get("get_areas_and_devices")
    result = await defn.handler(defn.params_model(), _ctx(ws=FakeWS()))
    assert result.status == "ok"
    kitchen = result.data["areas"]["Kitchen"]
    assert kitchen["devices"] == ["Hue Bulb"]
    assert kitchen["entities"] == ["light.kitchen"]
    bedroom = result.data["areas"]["Bedroom"]
    assert bedroom["entities"] == ["light.bedroom_lamp"]
    assert result.data["unassigned"]["entities"] == ["sensor.odd"]


async def test_topology_without_ws():
    defn = registry.get("get_areas_and_devices")
    result = await defn.handler(defn.params_model(), _ctx(ws=None))
    assert result.status == "error"
    assert result.error_code == "ws_unavailable"


async def test_automations_list():
    defn = registry.get("get_automations")
    result = await defn.handler(defn.params_model(), _ctx())
    assert result.status == "ok"
    rows = result.data["rows"]
    assert rows[0]["entity_id"] == "automation.night_lights"
    assert rows[0]["last_triggered"] == "2026-07-12T22:00:00+00:00"


async def test_automation_detail_fetches_config():
    defn = registry.get("get_automations")
    result = await defn.handler(
        defn.params_model(entity_id="automation.night_lights"), _ctx()
    )
    assert result.status == "ok"
    assert result.data["trigger"] == [{"platform": "sun"}]


async def test_automation_detail_yaml_defined():
    defn = registry.get("get_automations")
    result = await defn.handler(
        defn.params_model(entity_id="automation.yaml_one"), _ctx()
    )
    assert result.status == "ok"
    assert "not retrievable" in result.data["note"]


async def test_automation_detail_unknown():
    defn = registry.get("get_automations")
    result = await defn.handler(
        defn.params_model(entity_id="automation.nope"), _ctx()
    )
    assert result.status == "error"
    assert result.error_code == "entity_not_found"
    assert "automation.night_lights" in result.data["did_you_mean"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_read_topology.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `app/tools/read/get_areas_and_devices.py`**

```python
from pydantic import BaseModel

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    pass


async def handler(params: Params, ctx) -> ToolResult:
    if ctx.ws is None:
        return ToolResult.error(
            "ws_unavailable",
            "Area/device topology needs the websocket connection, which is not available.",
        )
    areas = await ctx.ws.request_cached("config/area_registry/list")
    devices = await ctx.ws.request_cached("config/device_registry/list")
    entities = await ctx.ws.request_cached("config/entity_registry/list")

    area_names = {a["area_id"]: a["name"] for a in areas}
    device_area = {d["id"]: d.get("area_id") for d in devices}
    device_name = {d["id"]: d.get("name_by_user") or d.get("name") or d["id"] for d in devices}

    out = {name: {"devices": [], "entities": []} for name in area_names.values()}
    unassigned = {"devices": [], "entities": []}

    for d in devices:
        bucket = out.get(area_names.get(d.get("area_id")))
        (bucket["devices"] if bucket else unassigned["devices"]).append(device_name[d["id"]])

    for e in entities:
        # entity's own area assignment overrides its device's area
        area_id = e.get("area_id") or device_area.get(e.get("device_id"))
        bucket = out.get(area_names.get(area_id))
        (bucket["entities"] if bucket else unassigned["entities"]).append(e["entity_id"])

    return ToolResult.ok({"areas": out, "unassigned": unassigned})


register(
    ToolDefinition(
        name="get_areas_and_devices",
        description="Get the whole-home topology: every area with its devices and entity_ids, plus unassigned ones. Use to answer 'what is in the living room' style questions.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
```

- [ ] **Step 4: Implement `app/tools/read/get_automations.py`**

```python
import difflib

from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult, bound_rows
from app.tools.registry import register


class Params(BaseModel):
    entity_id: str = Field(
        default="",
        description="Optional automation entity_id (e.g. 'automation.night_lights') to fetch the full config for. Empty lists all automations.",
    )


async def handler(params: Params, ctx) -> ToolResult:
    states = await ctx.rest.list_states()
    autos = [s for s in states if s["entity_id"].startswith("automation.")]

    if not params.entity_id:
        rows = [
            {
                "entity_id": a["entity_id"],
                "state": a["state"],
                "name": a.get("attributes", {}).get("friendly_name", ""),
                "last_triggered": a.get("attributes", {}).get("last_triggered"),
            }
            for a in autos
        ]
        return ToolResult.ok(bound_rows(rows, max_rows=ctx.settings.max_rows))

    match = next((a for a in autos if a["entity_id"] == params.entity_id), None)
    if match is None:
        ids = [a["entity_id"] for a in autos]
        return ToolResult.error(
            "entity_not_found",
            f"No automation {params.entity_id!r}.",
            data={"did_you_mean": difflib.get_close_matches(params.entity_id, ids, n=3, cutoff=0.4)},
        )

    automation_id = match.get("attributes", {}).get("id")
    if automation_id is None:
        return ToolResult.ok(
            {
                "entity_id": match["entity_id"],
                "state": match["state"],
                "attributes": match.get("attributes", {}),
                "note": "YAML-defined automation; full config not retrievable via the API.",
            }
        )
    config = await ctx.rest.get_automation_config(automation_id)
    return ToolResult.ok(config)


register(
    ToolDefinition(
        name="get_automations",
        description="List all automations (state, last_triggered) or, given an automation entity_id, fetch its full config (triggers, conditions, actions).",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_read_topology.py -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add app/tools/read/get_areas_and_devices.py app/tools/read/get_automations.py tests/test_read_topology.py
git commit -m "feat: area/device topology and automations read tools

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Skills package + `load_skill` tool + seed skill

**Files:**
- Create: `app/skills/__init__.py` (loader — replaces the empty file), `app/skills/diagnosing_automations.md`, `app/tools/read/load_skill.py`
- Test: `tests/test_skills.py`

**Interfaces:**
- Consumes: `ctx.skills_dir`.
- Produces: `app.skills.SkillMeta` (dataclass: `name: str`, `description: str`); `list_skills(skills_dir: Path) -> list[SkillMeta]`; `read_skill(skills_dir: Path, name: str) -> str | None`; registered tool `load_skill(name)`. Task 13 consumes `list_skills` for the prompt section.

- [ ] **Step 1: Write the failing tests**

`tests/test_skills.py`:
```python
from pathlib import Path

import pytest

from app.config import Settings
from app.skills import list_skills, read_skill
from app.tools import registry
from app.tools.context import ToolContext

SKILL = """---
name: test_skill
description: A test playbook
---
# Steps
Do the thing.
"""


@pytest.fixture
def skills_dir(tmp_path):
    (tmp_path / "test_skill.md").write_text(SKILL)
    return tmp_path


def test_list_skills(skills_dir):
    metas = list_skills(skills_dir)
    assert len(metas) == 1
    assert metas[0].name == "test_skill"
    assert metas[0].description == "A test playbook"


def test_read_skill(skills_dir):
    body = read_skill(skills_dir, "test_skill")
    assert body.startswith("# Steps")


def test_read_skill_missing(skills_dir):
    assert read_skill(skills_dir, "nope") is None


def test_seed_skill_is_valid():
    real_dir = Path("app/skills")
    metas = list_skills(real_dir)
    names = [m.name for m in metas]
    assert "diagnosing_automations" in names
    assert all(m.description for m in metas)


async def test_load_skill_tool(skills_dir):
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.load_skill",))
    defn = registry.get("load_skill")
    ctx = ToolContext(settings=Settings(_env_file=None), rest=None, ws=None, skills_dir=skills_dir)

    ok = await defn.handler(defn.params_model(name="test_skill"), ctx)
    assert ok.status == "ok"
    assert "Do the thing." in ok.data["content"]

    missing = await defn.handler(defn.params_model(name="nope"), ctx)
    assert missing.status == "error"
    assert missing.error_code == "skill_not_found"
    assert missing.data["available"] == ["test_skill"]
    registry._reset_for_tests()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_skills.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_skills'`.

- [ ] **Step 3: Implement `app/skills/__init__.py`**

```python
"""Skills: markdown playbooks with progressive disclosure. The system prompt
lists name+description one-liners; the model calls load_skill(name) to pull
the full text in only when relevant. Skills are data — iterating on playbooks
requires no code changes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(slots=True)
class SkillMeta:
    name: str
    description: str


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return yaml.safe_load(parts[1]) or {}, parts[2].strip()
    return {}, text.strip()


def list_skills(skills_dir: Path) -> list[SkillMeta]:
    metas = []
    for path in sorted(skills_dir.glob("*.md")):
        meta, _ = _parse_frontmatter(path.read_text())
        metas.append(
            SkillMeta(
                name=meta.get("name", path.stem),
                description=meta.get("description", ""),
            )
        )
    return metas


def read_skill(skills_dir: Path, name: str) -> str | None:
    for path in skills_dir.glob("*.md"):
        meta, body = _parse_frontmatter(path.read_text())
        if meta.get("name", path.stem) == name:
            return body
    return None
```

- [ ] **Step 4: Create `app/skills/diagnosing_automations.md`**

```markdown
---
name: diagnosing_automations
description: Playbook for finding out why an automation did not fire or misbehaved
---
# Diagnosing an automation

1. Call `get_automations` with the automation's entity_id.
   - If `state` is `off`, the automation is disabled — that is the answer.
   - Note `last_triggered` and compare it with when the user expected it to fire.
2. Fetch the config (same call) and identify the trigger entities and conditions.
3. Call `get_history` on each trigger entity over the window when the automation
   should have fired. Did the trigger condition actually occur?
4. Call `get_logbook` around the expected time — did the automation appear
   (fired but wrong action) or is it absent (never triggered)?
5. Call `get_error_log` and look for the automation's name or template errors.

Report back: whether the automation is enabled, whether its trigger actually
happened, whether it fired, and the single most likely cause.
```

- [ ] **Step 5: Implement `app/tools/read/load_skill.py`**

```python
from pydantic import BaseModel, Field

from app.skills import list_skills, read_skill
from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register


class Params(BaseModel):
    name: str = Field(description="Skill name exactly as listed in the system prompt")


async def handler(params: Params, ctx) -> ToolResult:
    body = read_skill(ctx.skills_dir, params.name)
    if body is None:
        available = [m.name for m in list_skills(ctx.skills_dir)]
        return ToolResult.error(
            "skill_not_found", f"No skill {params.name!r}.", data={"available": available}
        )
    return ToolResult.ok({"skill": params.name, "content": body})


register(
    ToolDefinition(
        name="load_skill",
        description="Load the full text of a skill playbook by name. Call this before starting a task that a listed skill covers.",
        params_model=Params,
        tier=Tier.READ,
        handler=handler,
    )
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_skills.py -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add app/skills/__init__.py app/skills/diagnosing_automations.md app/tools/read/load_skill.py tests/test_skills.py
git commit -m "feat: skills mechanism with load_skill tool and seed playbook

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: LLM factory (`app/agent/llm.py`)

**Files:**
- Create: `app/agent/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Settings` (Task 2).
- Produces: `build_llm(settings) -> BaseChatModel` — `ChatOllama` when `llm_provider == "ollama"`, else `ChatLiteLLM` with model string `f"{llm_provider}/{llm_model}"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_llm.py`:
```python
from langchain_ollama import ChatOllama

from app.agent.llm import build_llm
from app.config import Settings


def test_ollama_provider_builds_chat_ollama_with_options():
    s = Settings(
        _env_file=None,
        llm_provider="ollama", ollama_url="http://laptop:11434",
        llm_model="qwen3:8b", temperature=0.0, seed=7, reasoning=False,
        num_predict=1024, num_ctx=4096, keep_alive=-1,
    )
    llm = build_llm(s)
    assert isinstance(llm, ChatOllama)
    assert llm.base_url == "http://laptop:11434"
    assert llm.model == "qwen3:8b"
    assert llm.seed == 7
    assert llm.reasoning is False
    assert llm.num_predict == 1024
    assert llm.num_ctx == 4096
    assert llm.keep_alive == -1


def test_litellm_provider_builds_chat_litellm():
    from langchain_litellm import ChatLiteLLM

    s = Settings(
        _env_file=None,
        llm_provider="anthropic", llm_model="claude-haiku-4-5-20251001",
        api_key="sk-test",
    )
    llm = build_llm(s)
    assert isinstance(llm, ChatLiteLLM)
    assert llm.model == "anthropic/claude-haiku-4-5-20251001"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_llm.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `app/agent/llm.py`**

```python
"""LLM factory: local Ollama by default, any cloud provider via LiteLLM.
Everything downstream sees a BaseChatModel and stays provider-agnostic."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama

from app.config import Settings

# Generations on modest hardware can exceed a minute; never use httpx defaults.
OLLAMA_HTTP_TIMEOUT = 300


def build_llm(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "ollama":
        return ChatOllama(
            base_url=settings.ollama_url,
            model=settings.llm_model,
            temperature=settings.temperature,
            seed=settings.seed,
            reasoning=settings.reasoning,
            num_predict=settings.num_predict,
            num_ctx=settings.num_ctx,
            keep_alive=settings.keep_alive,
            client_kwargs={"timeout": OLLAMA_HTTP_TIMEOUT},
        )

    from langchain_litellm import ChatLiteLLM  # deferred: heavy import

    return ChatLiteLLM(
        model=f"{settings.llm_provider}/{settings.llm_model}",
        temperature=settings.temperature,
        api_key=settings.api_key or None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_llm.py -v`
Expected: 2 passed. If `ChatLiteLLM` field names differ (e.g. `model` attribute), adjust the *assertion* to the actual public attribute — not the factory.

- [ ] **Step 5: Commit**

```bash
git add app/agent/llm.py tests/test_llm.py
git commit -m "feat: LLM factory — ChatOllama with small-model knobs, ChatLiteLLM for cloud

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 13: Agent factory with trimming + time injection (`app/agent/factory.py`)

**Files:**
- Create: `app/agent/factory.py`
- Test: `tests/test_factory.py`

**Interfaces:**
- Consumes: `build_llm` (Task 12), `build_tools` (Task 7), `list_skills` (Task 11), `registry.load_all` (Task 4), `Settings`.
- Produces: `build_system_prompt(settings, skills_dir) -> str`; `timestamped_system(base_prompt) -> SystemMessage`; `trim_history(messages, max_tokens) -> list`; `ContextWindowMiddleware(base_prompt, max_tokens)`; `build_agent(settings, ctx, checkpointer=None)` (calls `registry.load_all()` if the registry is empty, then `create_agent` with the middleware). Tasks 14–16 call `build_agent`.

- [ ] **Step 1: Write the failing tests**

`tests/test_factory.py`:
```python
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.factory import (
    build_agent,
    build_system_prompt,
    timestamped_system,
    trim_history,
)
from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


def test_build_system_prompt_lists_skills(tmp_path):
    (tmp_path / "a_skill.md").write_text(
        "---\nname: a_skill\ndescription: does a thing\n---\nbody"
    )
    s = Settings(_env_file=None, system_prompt="Base prompt.")
    prompt = build_system_prompt(s, tmp_path)
    assert prompt.startswith("Base prompt.")
    assert "- a_skill: does a thing" in prompt


def test_build_system_prompt_no_skills(tmp_path):
    s = Settings(_env_file=None, system_prompt="Base prompt.")
    assert build_system_prompt(s, tmp_path) == "Base prompt."


def test_timestamped_system_injects_current_time():
    msg = timestamped_system("Base.")
    assert isinstance(msg, SystemMessage)
    assert msg.content.startswith("Base.")
    assert "Current time: 2026-" in msg.content


def test_trim_history_keeps_recent_and_starts_on_human():
    messages = []
    for i in range(50):
        messages.append(HumanMessage(f"question {i} " + "x" * 200))
        messages.append(AIMessage(f"answer {i} " + "y" * 200))
    trimmed = trim_history(messages, max_tokens=500)
    assert 0 < len(trimmed) < len(messages)
    assert trimmed[0].type == "human"
    assert trimmed[-1].content == messages[-1].content


def test_trim_history_never_returns_empty():
    huge = [HumanMessage("z" * 100000)]
    trimmed = trim_history(huge, max_tokens=10)
    assert trimmed == huge[-1:]


def test_build_agent_compiles_with_read_tools():
    registry._reset_for_tests()
    settings = Settings(_env_file=None, max_tier=1)
    ctx = ToolContext(settings=settings, rest=None, ws=None)
    agent = build_agent(settings, ctx)
    assert agent is not None
    # all eight read tools registered, none above tier 1
    assert len(registry.tools_for_tier(1)) == 8
    assert len(registry.tools_for_tier(2)) == 8
    registry._reset_for_tests()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_factory.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `app/agent/factory.py`**

```python
"""Builds the agent: LLM + tier-filtered tools + middleware that keeps a
small model healthy (history trimmed to a token budget, current time injected
into the system prompt on every call)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.checkpoint.memory import MemorySaver

from app.agent.llm import build_llm
from app.config import Settings
from app.skills import list_skills
from app.tools import registry
from app.tools.adapter import build_tools
from app.tools.context import ToolContext

# Reserve room for the model's own output and for tool schemas + prompt.
_RESPONSE_AND_SCHEMA_MARGIN = 2048


def build_system_prompt(settings: Settings, skills_dir: Path) -> str:
    prompt = settings.system_prompt
    metas = list_skills(skills_dir)
    if metas:
        lines = "\n".join(f"- {m.name}: {m.description}" for m in metas)
        prompt += (
            "\n\nAvailable skills (playbooks). Call load_skill(name) before "
            "starting a task one of them covers:\n" + lines
        )
    return prompt


def timestamped_system(base_prompt: str) -> SystemMessage:
    now = datetime.now().astimezone()
    return SystemMessage(
        f"{base_prompt}\n\nCurrent time: {now.isoformat(timespec='seconds')} ({now.tzname()})"
    )


def trim_history(messages: list, max_tokens: int) -> list:
    trimmed = trim_messages(
        messages,
        max_tokens=max_tokens,
        token_counter=count_tokens_approximately,
        strategy="last",
        start_on="human",
        include_system=False,
        allow_partial=False,
    )
    return trimmed or messages[-1:]


class ContextWindowMiddleware(AgentMiddleware):
    """Non-destructive per-call trimming (the checkpointed history is left
    intact) + fresh timestamp in the system prompt."""

    def __init__(self, base_prompt: str, max_tokens: int):
        super().__init__()
        self._base_prompt = base_prompt
        self._max_tokens = max_tokens

    def _prepare(self, request):
        return replace(
            request,
            messages=trim_history(list(request.messages), self._max_tokens),
            system_message=timestamped_system(self._base_prompt),
        )

    def wrap_model_call(self, request, handler):
        return handler(self._prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._prepare(request))


def build_agent(settings: Settings, ctx: ToolContext, checkpointer=None):
    if not registry.tools_for_tier(2):  # nothing registered yet
        registry.load_all()
    llm = build_llm(settings)
    tools = build_tools(ctx, settings.max_tier)
    base_prompt = build_system_prompt(settings, ctx.skills_dir)
    budget = max(1024, settings.num_ctx - settings.num_predict - _RESPONSE_AND_SCHEMA_MARGIN)
    middleware = ContextWindowMiddleware(base_prompt, budget)
    return create_agent(
        model=llm,
        tools=tools,
        middleware=[middleware],
        checkpointer=checkpointer or MemorySaver(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_factory.py -v`
Expected: 6 passed. If `replace(request, ...)` fails because `ModelRequest` provides its own `.override(...)` method instead, switch `_prepare` to `request.override(messages=..., system_message=...)` and re-run.

- [ ] **Step 5: Commit**

```bash
git add app/agent/factory.py tests/test_factory.py
git commit -m "feat: agent factory with context-window middleware (trimming + time injection)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 14: CLI REPL (`app/cli.py`)

**Files:**
- Create: `app/cli.py`

**Interfaces:**
- Consumes: `load_settings`, `RestClient`, `WebSocketClient`, `ToolContext`, `build_agent`.
- Produces: `python -m app.cli` — streaming REPL; the primary capability-testing interface.

This is thin wiring around tested components — verified manually (needs live HA + Ollama), no unit test.

- [ ] **Step 1: Implement `app/cli.py`**

```python
"""Capability-testing REPL. Streams tokens; tool calls appear via the
agent.tools audit log lines (INFO). Requires HA_BASE_URL, HA_TOKEN and an
Ollama server (see .env)."""

import asyncio
import logging

from langchain_core.messages import AIMessageChunk

from app.agent.factory import build_agent
from app.config import load_settings
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.tools.context import ToolContext


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    settings = load_settings()

    rest = RestClient(settings.ha_base_url, settings.ha_token)
    ws: WebSocketClient | None = WebSocketClient(settings.ws_url, settings.ha_token)
    try:
        await ws.start(connect_timeout=settings.ws_connect_timeout)
    except Exception as exc:
        print(f"warning: websocket unavailable ({exc}) — area/automation tools degraded")
        ws = None

    ctx = ToolContext(settings=settings, rest=rest, ws=ws)
    agent = build_agent(settings, ctx)
    config = {
        "configurable": {"thread_id": "cli"},
        "recursion_limit": settings.recursion_limit,
    }

    print(f"\nAgent ready ({settings.llm_model} via {settings.llm_provider}). Ctrl+C to quit.\n")
    try:
        while True:
            try:
                user_input = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nBye.")
                break
            if not user_input:
                continue
            print("Agent: ", end="", flush=True)
            async for token, _meta in agent.astream(
                {"messages": [{"role": "user", "content": user_input}]},
                config=config,
                stream_mode="messages",
            ):
                if isinstance(token, AIMessageChunk) and isinstance(token.content, str):
                    print(token.content, end="", flush=True)
            print("\n")
    finally:
        await rest.aclose()
        if ws is not None:
            await ws.stop()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify the full test suite still passes and the module imports**

```bash
venv/bin/python -m pytest -q
venv/bin/python -c "import app.cli; print('ok')"
```
Expected: all tests pass; `ok`.

- [ ] **Step 3: Manual smoke test (requires live HA + Ollama; .env with HA_BASE_URL, HA_TOKEN, OLLAMA_URL, LLM_MODEL)**

```bash
venv/bin/python -m app.cli
```
Ask: `What's the state of light.kitchen?` (any real entity). Expected: an `agent.tools tool=get_entity_state status=ok ...` log line, then a streamed answer. If no live setup is available right now, note it and move on — Task 16's eval harness covers this systematically.

- [ ] **Step 4: Commit**

```bash
git add app/cli.py
git commit -m "feat: streaming CLI REPL for capability testing

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 15: FastAPI app (`app/main.py`)

**Files:**
- Create: `app/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `load_settings`, `Settings`, `RestClient`, `WebSocketClient`, `ToolContext`, `build_agent`.
- Produces: `create_app(settings: Settings | None = None) -> FastAPI` (app factory — no import-time singletons). Endpoints: `POST /api/chat` (`{message, thread_id}` → `{reply}`), `GET /api/health` (`{status, ha, ollama, websocket}`), static frontend at `/`. Task 17's `run.sh` uses `uvicorn app.main:create_app --factory`.

- [ ] **Step 1: Write the failing tests**

`tests/test_main.py`:
```python
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.config import Settings
from app.main import create_app


def _settings():
    # Nothing listens on these ports: lifespan must degrade, not crash.
    return Settings(
        _env_file=None,
        ha_base_url="http://127.0.0.1:59999",
        ha_token="t",
        ollama_url="http://127.0.0.1:59998",
        ws_connect_timeout=0.5,
    )


class FakeAgent:
    async def ainvoke(self, payload, config=None):
        return {"messages": [AIMessage(content="hi there")]}


def test_health_degraded_without_backends():
    app = create_app(_settings())
    with TestClient(app) as client:
        resp = client.get("/api/health")
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "degraded"
    assert body["ha"] is False
    assert body["ollama"] is False
    assert body["websocket"] is False


def test_chat_returns_agent_reply():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeAgent()
        resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    assert resp.json() == {"reply": "hi there"}


def test_frontend_served_at_root():
    app = create_app(_settings())
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "html" in resp.headers["content-type"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_main.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `app/main.py`**

```python
"""Add-on entrypoint: FastAPI app factory. Run with
`uvicorn app.main:create_app --factory` — the factory keeps construction
config-injected and testable (no import-time singletons)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.factory import build_agent
from app.config import Settings, load_settings
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.tools.context import ToolContext

log = logging.getLogger("agent")


class ChatRequest(BaseModel):
    message: str
    thread_id: str = "default"


class ChatResponse(BaseModel):
    reply: str


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rest = RestClient(cfg.ha_base_url, cfg.ha_token)
        ws: WebSocketClient | None = WebSocketClient(cfg.ws_url, cfg.ha_token)
        try:
            await ws.start(connect_timeout=cfg.ws_connect_timeout)
        except Exception as exc:
            log.warning("websocket unavailable (%s) — area/automation tools degraded", exc)
            ws = None
        ctx = ToolContext(settings=cfg, rest=rest, ws=ws)
        app.state.settings = cfg
        app.state.rest = rest
        app.state.ws = ws
        app.state.agent = build_agent(cfg, ctx)
        yield
        await rest.aclose()
        if ws is not None:
            await ws.stop()

    app = FastAPI(lifespan=lifespan)

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        result = await app.state.agent.ainvoke(
            {"messages": [{"role": "user", "content": req.message}]},
            config={
                "configurable": {"thread_id": req.thread_id},
                "recursion_limit": app.state.settings.recursion_limit,
            },
        )
        return ChatResponse(reply=result["messages"][-1].content)

    @app.get("/api/health")
    async def health() -> dict:
        ha_ok = False
        ollama_ok = False
        try:
            ha_ok = await app.state.rest.ping()
        except Exception:
            pass
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{app.state.settings.ollama_url}/api/version")
                ollama_ok = resp.status_code == 200
        except Exception:
            pass
        ws_ok = app.state.ws is not None and app.state.ws.connected
        return {
            "status": "ok" if (ha_ok and ollama_ok) else "degraded",
            "ha": ha_ok,
            "ollama": ollama_ok,
            "websocket": ws_ok,
        }

    # Mounted last so /api/* wins. Frontend must use relative fetch paths
    # ("api/chat", not "/api/chat") — HA ingress serves us under a prefix.
    app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_main.py -v`
Expected: 3 passed. (The health test takes ~1 s from the ws connect timeout — acceptable.)

- [ ] **Step 5: Run the whole suite**

Run: `venv/bin/python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat: FastAPI app factory with chat and health endpoints

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 16: Eval harness (`tests/evals/`)

Scores tool-selection reliability of a live Ollama model: for each case, bind the real tool schemas, send one prompt, check the first tool call. No HA needed (handlers never execute). Deterministic via `temperature=0` + `seed`.

**Files:**
- Create: `tests/evals/__init__.py` (empty), `tests/evals/cases.yaml`, `tests/evals/run.py`

**Interfaces:**
- Consumes: `load_settings`, `build_llm`, `build_tools`, `build_system_prompt`, `registry.load_all`, `ToolContext`.
- Produces: `venv/bin/python -m tests.evals.run` → per-case PASS/FAIL + score. Not collected by pytest (no `test_` file names).

- [ ] **Step 1: Create `tests/evals/cases.yaml`**

```yaml
# Each case: one prompt, the tool the model MUST call first, and params that
# must match exactly (subset — unlisted params are not checked).
- id: entity_state_direct
  prompt: "What is the state of light.kitchen?"
  expect_tool: get_entity_state
  expect_params:
    entity_id: light.kitchen

- id: list_domain
  prompt: "Which lights do I have?"
  expect_tool: list_entities
  expect_params:
    domain: light

- id: list_area
  prompt: "What entities are in the Living room?"
  expect_tool: list_entities
  expect_params:
    area: Living room

- id: history_needs_timestamps
  prompt: "Show me the temperature history of sensor.living_room_temperature for the last 24 hours."
  expect_tool: get_history
  expect_params:
    entity_id: sensor.living_room_temperature

- id: logbook_recent
  prompt: "What happened in the house in the last hour?"
  expect_tool: get_logbook

- id: error_log
  prompt: "Is Home Assistant showing any errors?"
  expect_tool: get_error_log

- id: topology
  prompt: "What devices are in the bedroom?"
  expect_tool: get_areas_and_devices

- id: automations_list
  prompt: "List my automations and when they last ran."
  expect_tool: get_automations

- id: skill_loading
  prompt: "My automation automation.night_lights didn't fire last night, figure out why."
  expect_tool: load_skill
  expect_params:
    name: diagnosing_automations
```

- [ ] **Step 2: Create `tests/evals/run.py`**

```python
"""Tool-selection eval: does the configured model call the right tool with
the right params on the first turn? Usage:

    venv/bin/python -m tests.evals.run            # uses .env settings
    LLM_MODEL=llama3.1:8b venv/bin/python -m tests.evals.run
"""

import asyncio
import sys
from pathlib import Path

import yaml
from langchain_core.messages import HumanMessage

from app.agent.factory import build_system_prompt, timestamped_system
from app.agent.llm import build_llm
from app.config import load_settings
from app.tools import registry
from app.tools.adapter import build_tools
from app.tools.context import ToolContext

CASES_FILE = Path(__file__).parent / "cases.yaml"


def check(case: dict, tool_calls: list) -> tuple[bool, str]:
    if not tool_calls:
        return False, "no tool call"
    call = tool_calls[0]
    if call["name"] != case["expect_tool"]:
        return False, f"called {call['name']} (args {call['args']})"
    for key, expected in (case.get("expect_params") or {}).items():
        actual = call["args"].get(key)
        if actual != expected:
            return False, f"param {key}={actual!r}, expected {expected!r}"
    return True, ""


async def main() -> int:
    settings = load_settings()
    registry._reset_for_tests()
    registry.load_all()
    ctx = ToolContext(settings=settings, rest=None, ws=None)  # handlers never run
    tools = build_tools(ctx, max_tier=1)
    llm = build_llm(settings).bind_tools(tools)
    system = timestamped_system(build_system_prompt(settings, ctx.skills_dir))

    cases = yaml.safe_load(CASES_FILE.read_text())
    passed = 0
    print(f"model: {settings.llm_model} via {settings.llm_provider}\n")
    for case in cases:
        msg = await llm.ainvoke([system, HumanMessage(case["prompt"])])
        ok, reason = check(case, msg.tool_calls)
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'}  {case['id']}" + (f" — {reason}" if reason else ""))
    print(f"\nscore: {passed}/{len(cases)}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 3: Verify the runner loads (without hitting Ollama)**

```bash
venv/bin/python -c "
import yaml, pathlib
cases = yaml.safe_load(pathlib.Path('tests/evals/cases.yaml').read_text())
assert all({'id', 'prompt', 'expect_tool'} <= set(c) for c in cases), 'malformed case'
print(f'{len(cases)} cases ok')
"
venv/bin/python -m pytest -q   # evals must not be collected; suite still green
```
Expected: `9 cases ok`; pytest count unchanged from Task 15.

- [ ] **Step 4: Run against live Ollama (if reachable)**

```bash
venv/bin/python -m tests.evals.run
```
Expected: per-case PASS/FAIL and a score. A sub-perfect score is *information about the model*, not a plan failure — record the score in the commit message.

- [ ] **Step 5: Commit**

```bash
git add tests/evals/
git commit -m "feat: tool-selection eval harness

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 17: Add-on packaging (`config.yaml`, `run.sh`, `Dockerfile`, `README.md`)

**Files:**
- Modify: `config.yaml`, `run.sh`, `Dockerfile`, `README.md`

**Interfaces:**
- Consumes: `create_app` factory (Task 15), `Settings` field names (Task 2) — `options.json` keys must match them exactly.

- [ ] **Step 1: Rewrite `config.yaml`**

```yaml
name: "Local HA Agent (Ollama)"
version: "0.2.0"
slug: local_ha_agent
description: "Chat agent over Home Assistant state, powered by a local Ollama server (cloud models optional via LiteLLM)"
url: "https://github.com/yourname/homeassistant-ollama-agent"
arch:
  - amd64
  - aarch64
init: false
homeassistant_api: true
ingress: true
ingress_port: 8099
panel_icon: mdi:robot-outline
panel_title: "HA Agent"
watchdog: "http://[HOST]:[PORT:8099]/api/health"
options:
  llm_provider: "ollama"
  ollama_url: "http://192.168.1.50:11434"
  llm_model: "qwen2.5:7b"
  api_key: ""
  system_prompt: >-
    You are an assistant for this Home Assistant instance.
    Answer questions about the home using the available tools.
    Always look up real data with tools instead of guessing.
    Be concise and factual.
  max_tier: 1
  temperature: 0.0
  seed: 42
  reasoning: false
  num_predict: 2048
  num_ctx: 8192
  keep_alive: -1
  recursion_limit: 15
  max_rows: 50
schema:
  llm_provider: "str"
  ollama_url: "url"
  llm_model: "str"
  api_key: "password"
  system_prompt: "str"
  max_tier: "int(1,2)"
  temperature: "float(0,2)"
  seed: "int"
  reasoning: "bool"
  num_predict: "int(128,32768)"
  num_ctx: "int(1024,131072)"
  keep_alive: "int"
  recursion_limit: "int(2,50)"
  max_rows: "int(5,500)"
```

- [ ] **Step 2: Update `run.sh`**

```bash
#!/usr/bin/env bashio
cd /app || exit 1
exec uvicorn app.main:create_app --factory \
  --host 0.0.0.0 --port 8099 \
  --timeout-keep-alive 120
```
(Adjust the shebang/cd to match the existing `run.sh` and `Dockerfile` layout — keep whatever base-image convention is already there; the essential changes are `--factory` and `--timeout-keep-alive 120`.)

- [ ] **Step 3: Check `Dockerfile` still matches**

Read the existing `Dockerfile`; ensure it copies `app/` (including `app/skills/*.md`) and `frontend/`, installs `requirements.txt`, and runs `run.sh`. Update the copy paths if the restructure broke them.

- [ ] **Step 4: Update `README.md`**

Rewrite to describe: the architecture (registry/tiers/adapter, WS client, skills), dev setup (`.env` with `HA_BASE_URL`, `HA_TOKEN`, `OLLAMA_URL`, `LLM_MODEL`; `venv/bin/python -m app.cli`), the eval harness (`venv/bin/python -m tests.evals.run`), add-on installation (unchanged process), and the iteration roadmap (2: control tools behind `max_tier=2` + `allowed_domains`; 3: Assist + streaming UI). Keep the "verify tool-calling actually works" section — it's the eval harness's raison d'être. Keep it under ~80 lines.

- [ ] **Step 5: Full suite + final verification**

```bash
venv/bin/python -m pytest -q
venv/bin/python -c "from app.main import create_app; create_app; print('factory ok')"
```
Expected: all green; `factory ok`.

- [ ] **Step 6: Commit**

```bash
git add config.yaml run.sh Dockerfile README.md
git commit -m "chore: add-on packaging for iteration 1 — options schema, watchdog, factory entrypoint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes (already applied)

- **Spec coverage:** registry+tiers (T3–4), pydantic params (T3, T8–11), ToolResult envelope (T3, T7), bound_rows (T3, used T8–10), 8 read tools (T8–11), skills (T11), WS client + caching (T6), LLM factory + model knobs (T12), trimming + time injection (T13), audit log + loop guard (T7), timeouts (T12 `OLLAMA_HTTP_TIMEOUT`, T17 uvicorn), health + watchdog (T15, T17), CLI streaming (T14), eval harness (T16), config (T2, T17). Iteration-2 items (`call_service`, `control_entity`, `allowed_domains` enforcement) are intentionally *not* in this plan — `allowed_domains` exists in Settings only as a forward-declared field.
- **Known API risk points, with fallbacks stated inline:** `dataclasses.replace` on `ModelRequest` (T13 Step 4 fallback: `.override(...)`), `ChatLiteLLM` attribute names (T12 Step 4: adjust assertion, not factory).
- **Type consistency check:** `ToolContext(settings, rest, ws, skills_dir)` used identically in T7–16; `build_tools(ctx, max_tier)` (T7) consumed in T13/T16; `Settings` field names in T2 = `options.json` keys in T17; `start(connect_timeout=...)` (T6) used in T14/T15.
