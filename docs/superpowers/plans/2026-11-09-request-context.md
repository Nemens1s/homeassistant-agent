# Request Context (channel, satellite area, speaker) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gosling forwards `channel`, `device_id`, `satellite_id` and `user_id`. The agent resolves them to an area and a person, shows the model one context line on the current turn, corrects the fast-path `room` argument to the satellite's area when no room was spoken, and records the result in telemetry.

**Architecture:** Resolution runs at the HTTP edge (`ChatTurn`), with a timeout, and never raises. The resolved facts travel in the LangGraph `configurable` as a `RequestFacts` value on `RunScope`. A new innermost middleware, `RequestContextMiddleware`, sits just before `CurrentTimeMiddleware` and appends the line for the model call only. `FastPathMiddleware` reads the same facts plus the cached area registry to correct `room` inside its classify span. The needle payload does not change.

**Tech Stack:** FastAPI, pydantic v2, langchain v1 middleware (`langchain.agents.middleware.AgentMiddleware`), `langgraph.config.get_config`, OpenTelemetry, aiohttp (Gosling).

**Spec:** `/Users/ilniko/Desktop/Obsidian/Big Beautiful Brain/Claude Specs/homeassistant-agent/2026-10-26-request-context-design.md`

## Global Constraints

- The system prompt stays byte-identical between calls. The context line goes only on the latest user turn, before the current-time line (time stays last).
- Never mutate checkpointed messages. Use `model_copy` + `replace(request, messages=...)`, as `CurrentTimeMiddleware` does.
- Every resolution step fails open: no line, no error, no slow turn. The whole edge resolution is capped at **1.5 s** (`asyncio.wait_for`).
- All new wire fields are optional. A payload with no `channel` and no `context` behaves exactly as today. `ChatRequest` keeps `extra="ignore"`. `RequestContext` uses `extra="ignore"` too.
- `channel` values: `"assist"` (Gosling) and `"ui"` (frontend). The server defaults (`assist` for `/api/chat`, `ui` for `/api/chat/stream`) stay.
- The needle `/classify` payload does not change. The classifier sees the raw utterance only.
- Degraded mode (`CommandRouter`) and the events runner get no context.
- Plain readable Python: explicit `for` loops, no clever one-liners. Never raise into the agent loop.
- Verify langchain/HA APIs against the installed venv (`inspect`), not memory.
- Regular branches, no worktrees, no commits to `main`, no Claude co-author trailer. Agent repo branch `feat/request-context`. Gosling repo branch `feat/request-context`.
- Run `uv run pytest -q`. It must stay warning-free.

## Review Focus

1. **Area named with different spelling.** "turn on the living-room lights" or "kitchen's lights" from a bedroom satellite should count as naming a room. Normalise hyphens/apostrophes and allow a trailing `s` → Task 5 tests `test_names_a_room_tolerates_hyphen_and_possessive`.
2. **Satellite area not in the tool's `room` enum** (e.g. satellite in `hall`, script only allows `living_room`/`bedroom`). The call must fall through to the LLM, not fire in a wrong room → Task 5 `test_unsupported_satellite_area_falls_through`.
3. **HA slow or WS reconnecting during resolution.** The turn must not wait more than 1.5 s for context → Task 4 `test_slow_resolution_is_dropped`.
4. **Person entity without a `user_id` attribute, or a user with no person** (e.g. the HA admin account used by a script). No speaker, no error → Task 2 `test_person_no_match`.
5. **Multi-turn thread.** Turn 2 from a different satellite must use turn 2's area. Old user turns in history must carry no line, because only the latest human turn gets it → Task 3 `test_line_only_on_latest_user_turn`.

---

### Task 1: `RequestFacts` on `RunScope`

**Files:**
- Modify: `app/agent/run_scope.py`
- Test: `tests/test_run_scope.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) class RequestFacts: area_id: str | None = None; area_name: str | None = None; person_name: str | None = None; person_entity_id: str | None = None`, plus a property `is_empty -> bool` (true when all four are None).
  - `RunScope.request: RequestFacts = RequestFacts()` (new field, last).
  - Keys: `AREA_ID_KEY = "gosling_area_id"`, `AREA_NAME_KEY = "gosling_area_name"`, `PERSON_NAME_KEY = "gosling_person_name"`, `PERSON_ENTITY_KEY = "gosling_person_entity_id"`.
  - `facts_configurable(facts: RequestFacts) -> dict` returns only the non-None keys.
  - `scope_from_config(config)` also fills `request`. An area is kept only when both `area_id` and `area_name` are present. A person is kept only when both name and entity are present.
  - `scope_from_running_graph() -> RunScope` returns `scope_from_config(get_config())`, or `RunScope()` on `RuntimeError` (outside a graph run). Same pattern as `loop_guard_reset_middleware._current_thread_id`.

- [ ] **Step 1: Write failing tests** in `tests/test_run_scope.py`:

```python
def test_facts_configurable_drops_missing_values():
    facts = RequestFacts(area_id="living_room", area_name="Living Room")
    assert facts_configurable(facts) == {AREA_ID_KEY: "living_room", AREA_NAME_KEY: "Living Room"}
    assert facts_configurable(RequestFacts()) == {}

def test_scope_from_config_reads_request_facts():
    cfg = {"configurable": {"thread_id": "t", **facts_configurable(
        RequestFacts("bedroom", "Bedroom", "Ilja", "person.ilja"))}}
    assert scope_from_config(cfg).request == RequestFacts("bedroom", "Bedroom", "Ilja", "person.ilja")

def test_scope_from_config_without_facts_is_empty():
    assert scope_from_config({"configurable": {"thread_id": "t"}}).request.is_empty

def test_half_an_area_is_dropped():
    cfg = {"configurable": {AREA_ID_KEY: "bedroom"}}
    assert scope_from_config(cfg).request.area_id is None

def test_scope_from_running_graph_outside_a_run_is_default():
    assert scope_from_running_graph() == RunScope()
```

- [ ] **Step 2: Run** `uv run pytest tests/test_run_scope.py -q`. Expected: FAIL (ImportError on `RequestFacts`).
- [ ] **Step 3: Implement** the interfaces above in `app/agent/run_scope.py`. Update the module docstring to say it also carries the request's area/speaker.
- [ ] **Step 4: Run** `uv run pytest tests/test_run_scope.py -q`. Expected: PASS.
- [ ] **Step 5: Commit**: `git checkout -b feat/request-context` (first task only), then `git add app/agent/run_scope.py tests/test_run_scope.py && git commit -m "feat: carry request area/speaker facts on RunScope"`

---

### Task 2: Resolve device → area, user → person

**Files:**
- Create: `app/tools/helpers/request_context.py`
- Test: `tests/test_request_context_helper.py`

**Interfaces:**
- Consumes: `RequestFacts` (Task 1); `area_names(ctx)` from `app/tools/helpers/lookups.py`; `ws_ready(ctx)` from `app/tools/context.py`; `resolve_name(friendly_name, settings)` from `app/tools/helpers/people.py`.
- Produces:
  - `async def device_area(ctx, device_id: str | None) -> tuple[str, str] | None` returns `(area_id, area_name)`. Uses `ctx.ws.request_cached("config/device_registry/list")` and `area_names(ctx)`. Returns None when the id is missing, `not ws_ready(ctx)`, the device is unknown, the device has no `area_id`, the area id has no name, or anything raises.
  - `async def user_person(ctx, user_id: str | None) -> tuple[str, str] | None` returns `(canonical_name, entity_id)`. Uses `ctx.rest.list_states()`, keeps `person.*` states whose `attributes.user_id == user_id`, takes the first match, and names it with `resolve_name(friendly_name or entity_id, ctx.settings)[0]`. Returns None when nothing matches or anything raises. REST is used, so this works with WS down.
  - `async def resolve_request_facts(ctx, device_id: str | None, user_id: str | None) -> RequestFacts` combines both and never raises.

- [ ] **Step 1: Write failing tests.** Use a fake WS (`connected = True`, `async request_cached(msg_type, ttl=60.0)` returning fixtures by msg_type; see `tests/test_list_devices.py` for the shape) and a fake REST with `list_states`. Build `ToolContext(settings=Settings(_env_file=None, ha_base_url="http://x", ha_token="t"), rest=..., ws=...)`.
  - `test_device_area_found`: device `abc` with `area_id="living_room"`, area `{"area_id": "living_room", "name": "Living Room"}`. Expect `("living_room", "Living Room")`.
  - `test_device_without_area_is_none` (phone): device `area_id=None`. Expect `None`.
  - `test_unknown_device_is_none`.
  - `test_ws_none_is_none`: `ctx.ws = None`. Expect `None`.
  - `test_ws_error_is_none`: `request_cached` raises `RuntimeError`. Expect `None`.
  - `test_person_match`: states include `person.ilja` with `{"user_id": "u1", "friendly_name": "Ilja"}`. Expect `("Ilja", "person.ilja")`.
  - `test_person_no_match`: one person with no `user_id` attribute, one with `"u2"`. Expect `None` for `"u1"`.
  - `test_person_rest_error_is_none`.
  - `test_resolve_request_facts_combines_and_tolerates_none_ids`: `resolve_request_facts(ctx, None, None)` gives an empty result. With both ids it returns all four fields.
- [ ] **Step 2: Run** `uv run pytest tests/test_request_context_helper.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `app/tools/helpers/request_context.py`, with a module docstring in the style of `lookups.py` ("Not a tool module — registers nothing"). Use one `try/except Exception` per function, returning None. This is a deliberate fail-open boundary at the edge. Add a short comment saying so.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: resolve request device to area and user to person`

---

### Task 3: `RequestContextMiddleware`: the line on the latest user turn

**Files:**
- Create: `app/agent/middleware/request_context_middleware.py`
- Modify: `app/agent/middleware/__init__.py` (append it right before `CurrentTimeMiddleware()`, always on)
- Test: `tests/test_request_context_middleware.py`, plus an ordering assertion in `tests/test_factory.py`

**Interfaces:**
- Consumes: `RequestFacts`, `scope_from_running_graph()` (Task 1).
- Produces:
  - `def request_context_line(facts: RequestFacts) -> str | None`. Exact copy:
    - area + person: `Request context: the request came from a device in area "Living Room" (area_id: living_room); speaker: Ilja (person.ilja). If the user names a room, use that room instead.`
    - area only: `Request context: the request came from a device in area "Living Room" (area_id: living_room). If the user names a room, use that room instead.`
    - person only: `Request context: speaker: Ilja (person.ilja).`
    - empty → `None`
  - `def with_request_context(messages: list, line: str) -> list | None` has the same contract as `with_current_time`: a copy with `"\n\n" + line` appended to the latest human turn (str content), or a text part added (list content). Returns None when there is no human turn.
  - `class RequestContextMiddleware(AgentMiddleware)` with `awrap_model_call` / `wrap_model_call`. It reads `scope_from_running_graph().request`. With no line or no human turn, it calls the handler with the request unchanged. It never touches `system_message`.

- [ ] **Step 1: Write failing tests**
  - `test_line_area_and_person`, `test_line_area_only`, `test_line_person_only`, `test_line_empty_is_none`: assert the exact strings above.
  - `test_appends_line_without_mutating_original`: `msgs = [HumanMessage("dim the lights to 15%")]`. After `with_request_context(msgs, "X")`, `msgs[0].content == "dim the lights to 15%"` and `result[0].content == "dim the lights to 15%\n\nX"`.
  - `test_line_only_on_latest_user_turn`: `[Human("a"), AI("b"), Human("c")]`. Only index 2 changes.
  - `test_middleware_passes_through_when_no_facts`: monkeypatch `app.agent.middleware.request_context_middleware.scope_from_running_graph` to return `RunScope()`. The handler receives the same request object.
  - `test_middleware_appends_when_facts`: patch it to return `RunScope(request=RequestFacts("bedroom", "Bedroom", None, None))`. Build a real `ModelRequest` with the helper near `tests/test_factory.py:221` (the one the `CurrentTimeMiddleware` tests use). Assert the handler saw the line.
  - In `tests/test_factory.py`: `test_request_context_is_just_before_current_time`. With `build_middleware(...)`, the type names of the last two entries are `["RequestContextMiddleware", "CurrentTimeMiddleware"]`, and it is after `FastPathMiddleware` when fast path is on.
- [ ] **Step 2: Run** the new tests. Expected: FAIL.
- [ ] **Step 3: Implement.** Make `with_request_context` reuse the "find latest human index + copy" logic. If `current_time_middleware.with_current_time` can be split into a shared `append_to_latest_user_turn(messages, line) -> list | None` without changing its behaviour, do that and use it in both. Otherwise duplicate the small loop. Comment in `__init__.py`: inner to FastPath so the classifier never sees the line, outer to CurrentTime so the time stays last.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all PASS, no warnings.
- [ ] **Step 5: Commit** `feat: show the model the request's area and speaker`

---

### Task 4: Wire contract, edge resolution, telemetry, frontend channel

**Files:**
- Modify: `app/api/chat.py`, `app/telemetry/conventions.py`, `frontend/app.js:338`
- Test: `tests/test_request_context_api.py`

**Interfaces:**
- Consumes: `resolve_request_facts` (Task 2), `facts_configurable` (Task 1).
- Produces:
  - `class RequestContext(BaseModel)`: `model_config = {"extra": "ignore"}`; `device_id: str | None = None`, `satellite_id: str | None = None`, `user_id: str | None = None`.
  - `ChatRequest.context: RequestContext | None = None`.
  - `ChatTurn.resolve_context(self) -> Awaitable[None]` runs `resolve_request_facts(state.ctx, device_id, user_id)` under `asyncio.wait_for(..., timeout=1.5)`. Any exception or timeout gives an empty `RequestFacts` (log at debug). It is skipped when `req.context` is None or `getattr(state, "ctx", None)` is None. It stores `self.facts` and sets span attributes.
  - `ChatTurn.configurable()` returns `scope_configurable(...)` updated with `facts_configurable(self.facts)`.
  - Conventions (next to `GOSLING_DEVICE_ID`): `GOSLING_SATELLITE_ID = "gosling.context.satellite_id"`, `GOSLING_CONTEXT_AREA = "gosling.context.area"` (area_id), `GOSLING_CONTEXT_PERSON = "gosling.context.person"` (person entity_id). Set the existing `GOSLING_DEVICE_ID` from `context.device_id`; the `requests.device_id` column already reads it. Only set attributes whose value is not None, via `_set_span`.
  - Both endpoints call `await turn.resolve_context()` right after `translate_in()` and before the agent runs. Router turns skip it.
  - Frontend body: `JSON.stringify({ message: text, thread_id: threadId, channel: 'ui' })`.

- [ ] **Step 1: Write failing tests** (copy `_settings()` and the `RecordingAgent` pattern from `tests/test_main.py`). Inject the resolver by monkeypatching `app.api.chat.resolve_request_facts`.
  - `test_old_payload_still_accepted`: `{"message": "hello"}` returns 200, `reply == "hi there"`, and the configurable has no `gosling_area_id`.
  - `test_unknown_context_keys_ignored`: `context: {"device_id": "d", "foo": 1}` returns 200.
  - `test_context_facts_reach_configurable`: the resolver returns `RequestFacts("bedroom", "Bedroom", "Ilja", "person.ilja")`. The recorded configurable has `gosling_area_id == "bedroom"` and `gosling_person_entity_id == "person.ilja"`, and the resolver got `("d1", "u1")`.
  - `test_slow_resolution_is_dropped`: the resolver sleeps 5 s. The request finishes in under 3 s and the configurable has no area keys.
  - `test_resolver_error_is_dropped`: the resolver raises. Still 200.
  - `test_stream_endpoint_forwards_facts`: the same as `test_context_facts_reach_configurable` on `/api/chat/stream`, with a recording agent whose `astream` records `config` and yields one `AIMessage("ok")`.
  - `test_root_span_records_context`: InMemorySpanExporter pattern from `tests/test_i18n_telemetry.py`. The root span has `gosling.channel == "assist"`, `gosling.device_id == "d1"`, `gosling.context.satellite_id == "assist_satellite.living_room"`, `gosling.context.area == "bedroom"`, `gosling.context.person == "person.ilja"`.
  - `test_frontend_sends_ui_channel`: `"channel: 'ui'"` is in `Path("frontend/app.js").read_text()`.
- [ ] **Step 2: Run** `uv run pytest tests/test_request_context_api.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above. Import `resolve_request_facts` at module level in `chat.py` so the monkeypatch target exists.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all PASS, no warnings.
- [ ] **Step 5: Commit** `feat: accept request context on chat endpoints and record it`

---

### Task 5: Fast path: satellite area fills `room` when none is spoken

**Files:**
- Modify: `app/tools/helpers/request_context.py` (room matching), `app/agent/middleware/fast_path_middleware.py`, `app/agent/middleware/__init__.py` + `app/agent/factory.py` (pass `ws`), `app/telemetry/conventions.py`
- Test: `tests/test_request_context_helper.py`, `tests/test_fast_path_middleware.py`

**Interfaces:**
- Consumes: `scope_from_running_graph()` (Task 1).
- Produces:
  - `def room_terms(areas: list[dict], enum_values: list[str]) -> list[str]`: lower-cased terms from each area's `name`, `area_id` with `_`→space, every entry in `aliases`, and every enum value with `_`→space. Deduplicated and sorted longest first.
  - `def names_a_room(text: str, terms: list[str]) -> bool`: lower-cases the text, replaces `-` and `_` with spaces, and drops `'s`/`’s`. Then a word-bounded regex per term with an optional plural `s`: `rf"\b{re.escape(term)}s?\b"`.
  - `FastPathMiddleware.__init__(..., ws=None)`. `build_middleware(..., ws=None)` passes it, and `build_agent` passes `ws=ctx.ws`.
  - Conventions: `GOSLING_FP_ROOM_OVERRIDE = "gosling.fast_path.room_override"` with values `"overridden" | "kept" | "unsupported"`. It is set only when the correction step ran.
  - `async def _correct_room(self, decision, menu, message, span) -> Decision | None` is called inside `_classify`, in the span, only when the decision is accepted. Rules, in order:
    1. Return `decision` unchanged when `"room"` is not in `decision.arguments`, there is no satellite area (`scope_from_running_graph().request.area_id is None`), the menu item is not found, or `self._ws is None`.
    2. Read the areas via `self._ws.request_cached("config/area_registry/list")`. If that fails, return `decision` unchanged (today's behaviour).
    3. If `names_a_room(message, room_terms(areas, enum))` is true, set `kept` and return `decision`.
    4. If the item's `parameters.properties.room.enum` exists and does not contain the area_id, set `unsupported`, `GOSLING_FP_ACCEPTED=False` and `GOSLING_FP_SKIP_REASON="room_unsupported"`, and return None (the caller falls through to the handler).
    5. Otherwise return `dataclasses.replace(decision, arguments={**decision.arguments, "room": area_id})` and set `overridden`.
  - `_classify` returns None when `_correct_room` returns None, so `awrap_model_call` already falls through.

- [ ] **Step 1: Write failing tests**
  - Helper: `test_names_a_room_by_name_id_alias_and_enum` (areas: `Living Room`/`living_room` with alias `lounge`, `Bedroom`; enum `["living_room", "bedroom"]`). These are true: "dim the living room", "dim the lounge", "bedroom lights off". "dim the lights to 15%" is false.
  - Helper: `test_names_a_room_tolerates_hyphen_and_possessive`: "living-room lights" and "the bedroom's lamp" are both true.
  - Helper: `test_names_a_room_word_bounded`: area `Hall` doesn't match "hallway" (`hall` + optional `s` won't match `hallway`).
  - Middleware. Reuse `_MENU_WITH_REQUIRED`, with its enum changed to `["living_room", "bedroom"]` in a new menu constant. Use a fake ws with `request_cached` returning areas `living_room`, `bedroom`, `hall`. Monkeypatch `app.agent.middleware.fast_path_middleware.scope_from_running_graph`:
    - `test_satellite_area_overrides_invented_room`: decision args `{"room": "living_room", "percentage": 15}`, satellite `bedroom`, message "dim the lights to 15%". The tool call has `params == {"room": "bedroom", "percentage": 15}`.
    - `test_named_room_wins`: the same, with the message "dim the living room lights to 15%". `room == "living_room"`.
    - `test_no_satellite_area_leaves_call_untouched`: `RunScope()` → `room == "living_room"`.
    - `test_unsupported_satellite_area_falls_through`: satellite `hall`. The handler is called (use a handler that returns a sentinel AIMessage and assert it was returned).
    - `test_area_registry_error_leaves_call_untouched`: ws `request_cached` raises → original args.
    - `test_room_override_recorded_on_span`: with a recording fake tracer (copy the existing fast-path telemetry test's tracer helper), `gosling.fast_path.room_override == "overridden"`.
    - `test_classifier_sees_raw_utterance`: a backend that records `message` gets exactly "dim the lights to 15%".
- [ ] **Step 2: Run** `uv run pytest tests/test_request_context_helper.py tests/test_fast_path_middleware.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the interfaces above. Keep `_classify(message)`'s signature. Add the `_correct_room` call after the accepted attributes are set.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all PASS, no warnings.
- [ ] **Step 5: Commit** `feat: fast path uses the satellite's area when no room is named`

---

### Task 6: Docs

**Files:**
- Modify: `CLAUDE.md` (the `agent/run_scope.py` bullet: also carries the request's resolved area/speaker; add a one-line bullet for `RequestContextMiddleware` placement next to the factory note)

- [ ] **Step 1:** Edit the two bullets. Keep each to 1–2 lines.
- [ ] **Step 2: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 3: Commit** `docs: note request context in project guide`, then push the branch and open a PR (`gh pr create`) titled "Request context: channel, satellite area, speaker". Do not merge.

---

### Task 7: Gosling forwards channel + context (`~/IdeaProjects/hass-agent-gosling`)

**Files:**
- Modify: `custom_components/agent_gosling/client.py`, `custom_components/agent_gosling/conversation.py`
- Test: `tests/test_component_client.py`, `tests/test_component_conversation.py`

That repo has an uncommitted version bump (`manifest.json` / `pyproject.toml` → `0.2.0`). Branch with it in place and include it in this PR. It matches a feature release. Mention it in the PR description.

**Interfaces:**
- Produces:
  - `AgentApiClient.chat(self, text: str, conversation_id: str, context: dict | None = None) -> str`. The body is `{"message", "thread_id", "channel": "assist"}`, plus `"context": {...}` with only the non-None keys. `context` is omitted entirely when it would be empty.
  - `conversation.py`: `_request_context(user_input) -> dict` reads `getattr(user_input, "device_id", None)`, `getattr(user_input, "satellite_id", None)` and `getattr(getattr(user_input, "context", None), "user_id", None)`, then passes `context=` to `chat`.

- [ ] **Step 1: Verify the HA attribute names.** `cd ~/IdeaProjects/hass-agent-gosling && uv sync --group ha && uv run python -c "import dataclasses; from homeassistant.components.conversation import ConversationInput; print([f.name for f in dataclasses.fields(ConversationInput)])"`. Expect `device_id`, `satellite_id`, `context` in the list. If the harness won't install, keep the `getattr` defaults and say so in the PR.
- [ ] **Step 2: Write failing tests**
  - Client: the fake app echoes the whole body (`web.json_response({"reply": json.dumps(body)})`).
    - `test_chat_sends_assist_channel_without_context`: `chat("hi", "c1")` body is `{"message": "hi", "thread_id": "c1", "channel": "assist"}`.
    - `test_chat_sends_context_dropping_none`: `chat("hi", "c1", {"device_id": "d", "satellite_id": None, "user_id": "u"})` has body `context == {"device_id": "d", "user_id": "u"}`.
    - Update `test_chat_roundtrip` to the new echo format.
  - Conversation: `_StubClient.chat(self, text, conversation_id, context=None)` records the context. `test_forwards_device_satellite_and_user` builds a `ConversationInput` with `device_id="d1"`, `satellite_id="assist_satellite.living_room"` and `context=Context(user_id="u1")`, and expects the recorded context `{"device_id": "d1", "satellite_id": "assist_satellite.living_room", "user_id": "u1"}`. Existing tests keep passing.
- [ ] **Step 3: Run** `uv run pytest -q`. Expected: the new tests FAIL (HA tests skip if the harness is absent).
- [ ] **Step 4: Implement** the interfaces above.
- [ ] **Step 5: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 6: Commit and PR.** `git checkout -b feat/request-context`, commit `feat: forward channel and request context to the agent`, push, `gh pr create`. Do not merge.

---

## Out of scope (spec nice-to-have, not planned)

- An eval case for "dim the lights" with/without a room. The eval runner adds no request context today, so this would need runner support first.
