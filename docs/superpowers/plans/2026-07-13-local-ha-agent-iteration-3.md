# Local HA Agent — Iteration 3 Implementation Plan (Assist, Streaming, UI)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** HA Assist integration via a custom component, SSE streaming for the chat UI, a streaming-capable frontend, and optional SQLite conversation persistence, per `docs/superpowers/specs/2026-07-13-local-ha-agent-iteration-3-design.md`.

**PREREQUISITE:** the iteration-2 plan is fully executed and merged (this plan builds on its `ToolContext.audit` wiring and factory changes).

**Architecture:** The agent core is untouched. A translator module converts `agent.astream(stream_mode="messages")` into a typed event protocol consumed by a new SSE endpoint; the frontend reads it with `fetch` + `ReadableStream`. The Assist path is a separate deliverable: `custom_components/local_ha_agent/` (installed into HA core, not the addon) whose conversation entity forwards to the addon's existing non-streaming `/api/chat`.

**Tech Stack:** additions: `langgraph-checkpoint-sqlite` (3.x), `aiohttp` (component's HTTP client — HA-native), dev-only `pytest-homeassistant-custom-component` (fallback: plain aiohttp test server).

## Global Constraints

- Python: always `venv/bin/python`, `venv/bin/pip`, tests via `venv/bin/python -m pytest`.
- Commit after every task; trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Working directory: `/Users/ilniko/IdeaProjects/homeassistant-ollama-agent`, branch off `main` (e.g. `iteration-3`).
- SSE event protocol (exact shapes — frontend, endpoint, and tests all depend on them):
  `{"type":"token","text":...}` · `{"type":"tool_call","name":...,"args":{...}}` · `{"type":"tool_result","name":...,"status":"ok"|"error"}` · `{"type":"done","reply":...}` · `{"type":"error","message":...}`. `done` and `error` are terminal and mutually exclusive.
- `/api/chat` (non-streaming) must remain byte-compatible — it is the Assist component's contract.
- Frontend stays vanilla JS, no build step, no CDN dependencies, relative fetch paths (ingress prefix).
- The custom component imports NOTHING from `app/` (it runs inside HA core, a different process and Python env).

---

### Task 1: Checkpointer option (memory | sqlite)

**Files:**
- Modify: `requirements.txt`, `app/config.py`, `app/main.py`, `app/cli.py`
- Create: `app/agent/checkpoint.py`
- Test: `tests/test_checkpoint.py`

**Interfaces:**
- Produces: `Settings.checkpointer: str = "memory"` and `Settings.checkpoint_db_path: str = ""`; `open_checkpointer(settings)` — an async context manager yielding a `MemorySaver` (default) or `AsyncSqliteSaver` (when `checkpointer=="sqlite"` and a path is set); `build_agent(settings, ctx, checkpointer=...)` already accepts the result. `main.py` lifespan and `cli.py` wrap agent construction in it.

- [ ] **Step 1: Add dependency and failing tests**

Append `langgraph-checkpoint-sqlite==3.1.0` to `requirements.txt`; `venv/bin/pip install -r requirements.txt`.

`tests/test_checkpoint.py`:
```python
from langgraph.checkpoint.memory import MemorySaver

from app.agent.checkpoint import open_checkpointer
from app.config import Settings


async def test_default_is_memory_saver():
    async with open_checkpointer(Settings(_env_file=None)) as saver:
        assert isinstance(saver, MemorySaver)


async def test_sqlite_persists_across_reopen(tmp_path):
    db = str(tmp_path / "conv.db")
    s = Settings(_env_file=None, checkpointer="sqlite", checkpoint_db_path=db)
    cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    checkpoint = {"v": 4, "id": "chk-1", "ts": "2026-07-13T00:00:00+00:00",
                  "channel_values": {}, "channel_versions": {}, "versions_seen": {}}
    async with open_checkpointer(s) as saver:
        await saver.aput(cfg, checkpoint, {"source": "input", "step": 0, "parents": {}}, {})
    async with open_checkpointer(s) as saver:
        stored = await saver.aget(cfg)
        assert stored is not None and stored["id"] == "chk-1"


async def test_sqlite_without_path_falls_back_to_memory():
    s = Settings(_env_file=None, checkpointer="sqlite", checkpoint_db_path="")
    async with open_checkpointer(s) as saver:
        assert isinstance(saver, MemorySaver)
```
If `aput`'s checkpoint dict shape is rejected by the installed version, use the library's own `langgraph.checkpoint.base.empty_checkpoint()` helper instead — adjust the test, not the module.

- [ ] **Step 2: Run to verify failure** — `ImportError: app.agent.checkpoint`.

- [ ] **Step 3: Implement `app/agent/checkpoint.py`**

```python
"""Checkpointer selection: in-memory (default) or SQLite under /data so
conversations survive addon restarts. Trimming middleware bounds what
reaches the model, so growth here is a disk concern only."""

from __future__ import annotations

from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import MemorySaver

from app.config import Settings


@asynccontextmanager
async def open_checkpointer(settings: Settings):
    if settings.checkpointer == "sqlite" and settings.checkpoint_db_path:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_db_path) as saver:
            yield saver
    else:
        yield MemorySaver()
```

`app/config.py`: add `checkpointer: str = "memory"` and `checkpoint_db_path: str = ""` under "Agent behavior".

`app/main.py`: inside lifespan, wrap agent construction:
```python
            async with open_checkpointer(cfg) as saver:
                app.state.agent = build_agent(cfg, ctx, checkpointer=saver)
                yield
```
(the `async with` must enclose the `yield` so the sqlite connection lives for the app's lifetime; keep the existing outer try/finally teardown). `app/cli.py`: wrap the REPL body equivalently.

- [ ] **Step 4: Verify** — `venv/bin/python -m pytest tests/test_checkpoint.py -v`, then full suite (main/cli lifespan tests must still pass).

- [ ] **Step 5: Commit**

```bash
git add requirements.txt app/config.py app/agent/checkpoint.py app/main.py app/cli.py tests/test_checkpoint.py
git commit -m "feat: optional SQLite conversation checkpointer

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Stream translator (`app/agent/streaming.py`)

**Files:**
- Create: `app/agent/streaming.py`
- Test: `tests/test_streaming.py`

**Interfaces:**
- Consumes: an agent exposing `astream(payload, config=..., stream_mode="messages")` yielding `(message, metadata)` tuples.
- Produces: `stream_events(agent, message: str, thread_id: str, recursion_limit: int) -> AsyncIterator[dict]` emitting protocol events (Global Constraints); terminal `done` carries the assembled reply; `GraphRecursionError` → terminal `error`.

- [ ] **Step 1: Write the failing tests** (`tests/test_streaming.py`)

```python
import json

from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError

from app.agent.streaming import stream_events


class FakeAgent:
    def __init__(self, script):
        self._script = script

    async def astream(self, payload, config=None, stream_mode=None):
        assert stream_mode == "messages"
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item


async def _collect(agent):
    return [e async for e in stream_events(agent, "hi", "t1", 15)]


async def test_token_and_done_events():
    agent = FakeAgent([
        (AIMessageChunk(content="The "), {}),
        (AIMessageChunk(content="light is on."), {}),
    ])
    events = await _collect(agent)
    assert events[0] == {"type": "token", "text": "The "}
    assert events[-1] == {"type": "done", "reply": "The light is on."}


async def test_tool_call_and_result_events():
    chunk = AIMessageChunk(content="")
    chunk.tool_calls = [{"name": "get_entity_state",
                         "args": {"entity_id": "light.kitchen"}, "id": "c1"}]
    tool_msg = ToolMessage(content=json.dumps({"status": "ok", "data": {}}),
                           tool_call_id="c1", name="get_entity_state")
    agent = FakeAgent([(chunk, {}), (tool_msg, {}),
                       (AIMessageChunk(content="done"), {})])
    events = await _collect(agent)
    types = [e["type"] for e in events]
    assert types == ["tool_call", "tool_result", "token", "done"]
    assert events[0]["name"] == "get_entity_state"
    assert events[1]["status"] == "ok"


async def test_recursion_error_becomes_terminal_error_event():
    agent = FakeAgent([(AIMessageChunk(content="thinking"), {}),
                       GraphRecursionError("limit")])
    events = await _collect(agent)
    assert events[-1]["type"] == "error"
    assert "15" in events[-1]["message"]
    assert not any(e["type"] == "done" for e in events)


async def test_tool_result_error_status():
    tool_msg = ToolMessage(content=json.dumps({"status": "error",
                                               "error": {"code": "x", "message": "y"}}),
                           tool_call_id="c1", name="get_history")
    agent = FakeAgent([(tool_msg, {}), (AIMessageChunk(content="sorry"), {})])
    events = await _collect(agent)
    assert events[0] == {"type": "tool_result", "name": "get_history", "status": "error"}
```

- [ ] **Step 2: Run to verify failure** — `ImportError`.

- [ ] **Step 3: Implement `app/agent/streaming.py`**

```python
"""Translates the agent's message stream into the SSE event protocol shared
by the endpoint and the frontend. The CLI keeps its simpler inline filter —
two consumers with different presentations; revisit only if a third appears."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.errors import GraphRecursionError


async def stream_events(
    agent, message: str, thread_id: str, recursion_limit: int
) -> AsyncIterator[dict]:
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    parts: list[str] = []
    try:
        async for msg, _meta in agent.astream(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
            stream_mode="messages",
        ):
            if isinstance(msg, AIMessageChunk):
                for tc in msg.tool_calls:
                    yield {"type": "tool_call", "name": tc["name"], "args": tc["args"]}
                if isinstance(msg.content, str) and msg.content:
                    parts.append(msg.content)
                    yield {"type": "token", "text": msg.content}
            elif isinstance(msg, ToolMessage):
                try:
                    status = json.loads(msg.content).get("status", "ok")
                except (TypeError, ValueError):
                    status = "ok"
                yield {"type": "tool_result", "name": msg.name or "", "status": status}
    except GraphRecursionError:
        yield {
            "type": "error",
            "message": (
                f"Stopped after {recursion_limit} steps without reaching an answer. "
                "Try a more specific question."
            ),
        }
        return
    yield {"type": "done", "reply": "".join(parts)}
```

- [ ] **Step 4: Verify** — `venv/bin/python -m pytest tests/test_streaming.py -v`, full suite green.

- [ ] **Step 5: Commit**

```bash
git add app/agent/streaming.py tests/test_streaming.py
git commit -m "feat: agent stream to SSE event protocol translator

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: SSE endpoint (`POST /api/chat/stream`)

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `stream_events` (Task 2).
- Produces: `POST /api/chat/stream` accepting the same `ChatRequest` body, responding `text/event-stream` where each event is `data: <json>\n\n`; header `Cache-Control: no-cache`. `/api/chat` untouched.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_main.py`)

```python
def _parse_sse(text):
    return [json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")]


class FakeStreamAgent:
    async def astream(self, payload, config=None, stream_mode=None):
        from langchain_core.messages import AIMessageChunk
        yield AIMessageChunk(content="hel"), {}
        yield AIMessageChunk(content="lo"), {}


def test_chat_stream_emits_protocol_events():
    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = FakeStreamAgent()
        resp = client.post("/api/chat/stream", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert events[0] == {"type": "token", "text": "hel"}
    assert events[-1] == {"type": "done", "reply": "hello"}


def test_chat_stream_recursion_limit_is_error_event():
    from langgraph.errors import GraphRecursionError

    class Exploding:
        async def astream(self, payload, config=None, stream_mode=None):
            raise GraphRecursionError("limit")
            yield  # pragma: no cover — makes this an async generator

    app = create_app(_settings())
    with TestClient(app) as client:
        client.app.state.agent = Exploding()
        resp = client.post("/api/chat/stream", json={"message": "hi"})
    events = _parse_sse(resp.text)
    assert events[-1]["type"] == "error"
```
(add `import json` to the test imports if missing)

- [ ] **Step 2: Run to verify failure** — 404 on the new route.

- [ ] **Step 3: Implement in `app/main.py`** (inside `create_app`, before the static mount)

```python
    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        async def sse() -> AsyncIterator[str]:
            async for event in stream_events(
                app.state.agent,
                req.message,
                req.thread_id,
                app.state.settings.recursion_limit,
            ):
                yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
```
Imports: `json`, `AsyncIterator` from `collections.abc`, `StreamingResponse` from `fastapi.responses`, `stream_events` from `app.agent.streaming`.

- [ ] **Step 4: Verify** — `venv/bin/python -m pytest tests/test_main.py -v`, full suite green.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main.py
git commit -m "feat: SSE chat streaming endpoint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Streaming frontend

**Files:**
- Modify: `frontend/index.html` (full rewrite; keep it the only frontend file)

**Interfaces:**
- Consumes: `POST api/chat/stream` (RELATIVE path — ingress) with the Task 3 protocol.
- Produces: streaming chat UI. No unit tests (backend protocol is tested in Tasks 2–3); verification is the checklist below.

- [ ] **Step 1: Rewrite `frontend/index.html`** — single file, vanilla JS, structured as:

- `<main>` message list + fixed bottom form (input + Send + "New conversation" button).
- Thread id: `sessionStorage.getItem("thread") ?? crypto.randomUUID()` persisted back; "New conversation" replaces it and clears the list.
- Send flow: `fetch("api/chat/stream", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({message, thread_id})})`, then read `resp.body.getReader()`, decode with `TextDecoder`, split on `\n\n`, parse `data: ` lines (buffer partial frames across chunks).
- Event handling: `token` → append to the current assistant bubble; `tool_call` → add a collapsed activity line `🔍 <name>` above the bubble; `tool_result` with `status:"error"` → mark that line ⚠️; `done` → finalize (render markdown); `error` → red inline message.
- Markdown-lite renderer (hand-rolled, ~30 lines): escape HTML first, then \`\`\`code blocks\`\`\`, \`inline code\`, `**bold**`, and `- ` lists. No external libraries.
- Non-2xx or fetch failure → inline error bubble.

- [ ] **Step 2: Static verification**

```bash
venv/bin/python -m pytest -q                      # suite untouched
venv/bin/python - <<'EOF'
text = open("frontend/index.html").read()
for needle in ("api/chat/stream", "sessionStorage", "getReader", "randomUUID"):
    assert needle in text, needle
assert "http://" not in text and "https://" not in text, "no absolute/CDN URLs"
print("frontend static checks ok")
EOF
```

- [ ] **Step 3: Manual checklist** (record results in the task report; needs live HA+Ollama or the addon)

- Tokens render incrementally; tool indicator appears then collapses.
- "New conversation" resets context (agent forgets prior turn).
- Works under the ingress path prefix (no leading-slash fetches).
- A long answer (>60 s generation) completes without connection termination.
If no live environment is reachable, state that explicitly and leave the checklist to the branch-finish verification.

- [ ] **Step 4: Commit**

```bash
git add frontend/index.html
git commit -m "feat: streaming chat UI with tool activity and per-session threads

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Custom component — API client + scaffold

**Files:**
- Create: `custom_components/local_ha_agent/__init__.py`, `manifest.json`, `const.py`, `client.py`, `hacs.json` (repo root)
- Test: `tests/test_component_client.py`

**Interfaces:**
- Produces: `AgentApiClient(base_url: str, session: aiohttp.ClientSession, timeout: float = 90.0)` with `async chat(text: str, conversation_id: str) -> str` (POST `{base_url}/api/chat`, returns `reply`; raises `AgentApiError` on non-2xx/network/timeout). The entity (Task 6) consumes this. The component imports nothing from `app/`.

- [ ] **Step 1: Scaffold files**

`custom_components/local_ha_agent/manifest.json`:
```json
{
  "domain": "local_ha_agent",
  "name": "Local HA Agent",
  "codeowners": [],
  "config_flow": true,
  "dependencies": ["conversation"],
  "documentation": "https://github.com/Nemens1s/homeassistant-ollama-agent",
  "iot_class": "local_polling",
  "requirements": [],
  "version": "0.1.0"
}
```

`const.py`:
```python
DOMAIN = "local_ha_agent"
CONF_BASE_URL = "base_url"
DEFAULT_BASE_URL = "http://local-ha-agent:8099"
```

`hacs.json` (repo root):
```json
{"name": "Local HA Agent", "content_in_root": false}
```

`__init__.py`:
```python
"""Local HA Agent: exposes the addon's agent as an Assist conversation agent."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS = [Platform.CONVERSATION]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

- [ ] **Step 2: Write the failing client tests** (`tests/test_component_client.py`)

Add `aiohttp` to `requirements-dev.txt` and `venv/bin/pip install -r requirements-dev.txt`.

```python
import aiohttp
import pytest
from aiohttp import web

from custom_components.local_ha_agent.client import AgentApiClient, AgentApiError


@pytest.fixture
async def fake_addon(aiohttp_server_factory=None):
    async def chat(request):
        body = await request.json()
        if body["message"] == "boom":
            return web.Response(status=500)
        return web.json_response({"reply": f"echo:{body['message']}:{body['thread_id']}"})

    app = web.Application()
    app.router.add_post("/api/chat", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


async def test_chat_roundtrip(fake_addon):
    async with aiohttp.ClientSession() as session:
        client = AgentApiClient(fake_addon, session)
        reply = await client.chat("hello", "conv-1")
    assert reply == "echo:hello:conv-1"


async def test_http_error_raises_agent_api_error(fake_addon):
    async with aiohttp.ClientSession() as session:
        client = AgentApiClient(fake_addon, session)
        with pytest.raises(AgentApiError):
            await client.chat("boom", "conv-1")


async def test_unreachable_raises_agent_api_error():
    async with aiohttp.ClientSession() as session:
        client = AgentApiClient("http://127.0.0.1:59997", session, timeout=1.0)
        with pytest.raises(AgentApiError):
            await client.chat("hello", "conv-1")
```

- [ ] **Step 3: Run to verify failure**, then implement `client.py`

```python
"""HTTP client for the addon API. Kept free of Home Assistant imports so it
is unit-testable without the HA harness."""

from __future__ import annotations

import asyncio

import aiohttp


class AgentApiError(Exception):
    """Addon unreachable or returned an error."""


class AgentApiClient:
    def __init__(self, base_url: str, session: aiohttp.ClientSession, timeout: float = 90.0):
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def chat(self, text: str, conversation_id: str) -> str:
        try:
            async with self._session.post(
                f"{self._base_url}/api/chat",
                json={"message": text, "thread_id": conversation_id},
                timeout=self._timeout,
            ) as resp:
                if resp.status != 200:
                    raise AgentApiError(f"addon returned HTTP {resp.status}")
                data = await resp.json()
                return data["reply"]
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise AgentApiError(f"addon unreachable: {exc}") from exc
```

- [ ] **Step 4: Verify** — `venv/bin/python -m pytest tests/test_component_client.py -v`; full suite green (the component's other files are not imported by any test yet, so the missing `homeassistant` package cannot break collection — confirm `pytest -q` collects cleanly).

- [ ] **Step 5: Commit**

```bash
git add custom_components/ hacs.json requirements-dev.txt tests/test_component_client.py
git commit -m "feat: custom component scaffold and HA-free addon API client

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Conversation entity + config flow

**Files:**
- Create: `custom_components/local_ha_agent/conversation.py`, `config_flow.py`, `strings.json`
- Test: `tests/test_component_conversation.py` (harness-dependent; see Step 3 fallback)

**Interfaces:**
- Consumes: `AgentApiClient` (Task 5); HA's `conversation.ConversationEntity` API.
- Produces: a conversation entity forwarding to the addon (`conversation_id` → `thread_id`), speaking errors instead of raising; a one-field config flow (base URL).

- [ ] **Step 1: Implement `conversation.py`**

```python
"""Conversation entity: forwards Assist input to the addon agent."""

from __future__ import annotations

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import ulid

from .client import AgentApiClient, AgentApiError
from .const import CONF_BASE_URL, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    client = AgentApiClient(entry.data[CONF_BASE_URL], async_get_clientsession(hass))
    async_add_entities([LocalAgentConversationEntity(entry, client)])


class LocalAgentConversationEntity(conversation.ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_languages = MATCH_ALL

    def __init__(self, entry: ConfigEntry, client: AgentApiClient) -> None:
        self._client = client
        self._attr_unique_id = entry.entry_id

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        conversation_id = user_input.conversation_id or ulid.ulid_now()
        response = intent.IntentResponse(language=user_input.language)
        try:
            reply = await self._client.chat(user_input.text, conversation_id)
            response.async_set_speech(reply)
        except AgentApiError as err:
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"The local agent is not reachable: {err}",
            )
        return conversation.ConversationResult(
            response=response, conversation_id=conversation_id
        )
```

`config_flow.py`:
```python
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN


class LocalHaAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Local HA Agent", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): str}
            ),
        )
```

`strings.json`:
```json
{
  "config": {
    "step": {
      "user": {
        "data": {"base_url": "Addon base URL"},
        "description": "URL where the Local HA Agent addon API is reachable from Home Assistant."
      }
    }
  }
}
```

**The HA API surface above must be verified, not trusted** — it targets HA 2025/2026 conversation platform. Step 3's harness (or, failing that, reading the installed harness's `homeassistant/components/conversation/entity.py`) is the verification; adjust names (`ConfigFlowResult`, `async_set_error` signature, `ulid` helper) to what the installed HA version exposes and record every adjustment in the report.

- [ ] **Step 2: Attempt the HA test harness**

Create `requirements-dev-ha.txt` containing `pytest-homeassistant-custom-component==0.13.346` and try `venv/bin/pip install -r requirements-dev-ha.txt` (installs a pinned `homeassistant`). **If resolution fails on Python 3.14, skip to Step 3's fallback** and record the failure output.

- [ ] **Step 3: Tests — harness path OR fallback**

Harness path (`tests/test_component_conversation.py`): use the harness's `hass` fixture; set up a config entry with `CONF_BASE_URL` pointing at the Task-5 fake addon server; call `conversation.async_converse(hass, "what's on?", None, Context(), agent_id=<entity id>)`; assert the speech equals the fake reply and that a second converse with the returned `conversation_id` sends the same `thread_id` to the fake server.

Fallback path (no HA install): unit-test `async_process` directly by stubbing the `homeassistant.*` modules in `sys.modules` before import is NOT acceptable (too brittle) — instead limit automated coverage to Task 5's client tests, add the entity to the manual checklist in Task 7, and record the downgrade prominently in the report. The entity file must still pass `venv/bin/python -m py_compile custom_components/local_ha_agent/conversation.py` (syntax gate) — note that py_compile does not validate the HA API names.

- [ ] **Step 4: Full suite** — `venv/bin/python -m pytest -q` green either way (harness tests live in their own file; if the harness is installed, run its file explicitly too).

- [ ] **Step 5: Commit**

```bash
git add custom_components/local_ha_agent/ tests/ requirements-dev-ha.txt
git commit -m "feat: Assist conversation entity and config flow

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs, packaging, end-to-end checklist

**Files:**
- Modify: `README.md`, `config.yaml` (version bump only), `docs/superpowers/specs/2026-07-13-local-ha-agent-iteration-3-design.md` (only if reality diverged — record deviations)

**Interfaces:** none new — documentation and verification.

- [ ] **Step 1: README** — add an "Assist integration" section: copy `custom_components/local_ha_agent/` into HA's `config/custom_components/` (or add the repo as a HACS custom repository), restart HA, add the integration, set the addon URL, then select "Local HA Agent" as the conversation agent in a voice assistant pipeline. Add the "Assist wants a fast model" note (90 s component timeout; long questions belong in the chat UI/REPL). Add a "Streaming & persistence" paragraph: `checkpointer: sqlite` option, `/data/conversations.db`, and the SSE endpoint. Bump addon `version` in `config.yaml` to `"0.4.0"`.

- [ ] **Step 2: Full suite + factory smoke**

```bash
venv/bin/python -m pytest -q
venv/bin/python -c "from app.main import create_app; create_app; print('factory ok')"
```

- [ ] **Step 3: End-to-end manual checklist** (record pass/fail/skipped per item in the report; requires live HA + Ollama)

1. Addon rebuilt and started; `/api/health` ok.
2. Chat UI streams tokens; tool indicator appears; >60 s generation survives.
3. `checkpointer: sqlite` set → restart addon → follow-up question retains context.
4. Component installed in HA; config flow completes; Assist text chat answers via the agent; follow-up in the same Assist conversation shares context.
5. Addon stopped → Assist replies with the spoken-friendly error, no traceback in HA logs.

- [ ] **Step 4: Commit**

```bash
git add README.md config.yaml docs/
git commit -m "docs: Assist install guide, streaming and persistence notes; addon 0.4.0

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes (already applied)

- **Spec coverage:** checkpointer option (T1), SSE protocol + translator (T2), endpoint (T3), UI incl. markdown-lite/thread/new-conversation/tool indicators (T4), component scaffold + HA-free client (T5), conversation entity + config flow + error shaping (T6), install docs + latency note + e2e checklist incl. >60 s SSE and restart-persistence (T7). Security note from the spec (unauthenticated component→addon hop) lands in the README section (T7 Step 1 — include the sentence).
- **Type consistency:** event dict shapes identical in T2 implementation, T2 tests, T3 tests, and T4 frontend handling; `ChatRequest` reused by both endpoints; `AgentApiClient.chat(text, conversation_id)` identical in T5 tests and T6 entity.
- **Known API risks, fallbacks stated inline:** checkpoint dict shape (T1: use `empty_checkpoint()`), HA conversation/config-flow surface (T6: verify against installed harness, record adjustments), harness-on-py3.14 (T6 Step 2/3 fallback), `FakeAgent` duck-typing for astream (T2/T3 tests own their fakes).
- **Ordering:** T2→T3→T4 form the streaming chain; T5→T6 the component chain (independent of streaming); T1 first because main.py's lifespan changes twice otherwise.
