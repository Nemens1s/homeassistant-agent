# Automation-mediated Control with a Home-level AI Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent act only through a curated menu of `automation.ai_*` automations, gated globally by a fail-closed point-read of `input_boolean.ai_triggered_actions`, and retire direct entity control.

**Architecture:** The adapter seam (`app/tools/adapter.py`) enforces one master gate before any ACTION-tier handler runs. `trigger_automation` becomes the only action tool and refuses anything not matching the `automation.ai_` prefix. `control_entity` and the whole label guardrail are deleted. Discovery (`get_automations`) marks menu items with an `ai_controllable` flag.

**Tech Stack:** Python 3.14, pydantic / pydantic-settings, langchain v1 (`create_agent`), httpx, pytest. Run everything with `venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-08-24-automation-mediated-control-design.md`

## Global Constraints

- **Interpreter:** ALWAYS `venv/bin/python` / `venv/bin/pip`. Never system python.
- **Tests:** `venv/bin/python -m pytest -q` — no HA/Ollama needed. Exactly **2** known third-party warnings expected (langchain pydantic-v1 shim, starlette TestClient deprecation). Any new warning is a finding.
- **Tool output:** always `ToolResult(...).to_json()` compact JSON — never `str(dict)`, never raw exceptions. Handlers never raise into the agent loop; all error mapping lives in the adapter.
- **Style:** plain, readable Python — regular `for` loops over comprehensions, explicit steps over clever one-liners.
- **langchain v1 API only** — verify against the installed venv, don't trust training data.
- **Read-only-by-construction stays:** the GET-only REST client is unchanged; writes still flow solely through `call_service`.
- **Branch:** work happens on `automation-mediated-control` (already created; the spec is committed there).
- **Prefix constant:** the AI menu prefix is the string `"automation.ai_"`, defined once as `AI_AUTOMATION_PREFIX` in `app/tools/action/trigger_automation.py` and imported wherever else needed.

---

### Task 1: Add the `ai_actions_switch` setting

Adds the configurable master-switch entity id. Additive and independent — nothing reads it yet.

**Files:**
- Modify: `app/config.py` (Settings, Agent behavior block near `allowed_domains`)
- Modify: `config.yaml` (options + schema)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.ai_actions_switch: str` (default `"input_boolean.ai_triggered_actions"`; empty string disables the gate).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`, inside `test_defaults`:

```python
    assert s.ai_actions_switch == "input_boolean.ai_triggered_actions"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_config.py::test_defaults -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'ai_actions_switch'`.

- [ ] **Step 3: Add the setting**

In `app/config.py`, in the `# Agent behavior` block, immediately after the `allowed_domains` line, add:

```python
    ai_actions_switch: str = "input_boolean.ai_triggered_actions"  # master gate; "" disables
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Mirror in the addon options schema**

In `config.yaml`, under `options:` add (after `allowed_domains:` list, before `temperature`):

```yaml
  ai_actions_switch: "input_boolean.ai_triggered_actions"
```

and under `schema:` add (after `allowed_domains: ["str"]`):

```yaml
  ai_actions_switch: "str"
```

- [ ] **Step 6: Full suite + commit**

Run: `venv/bin/python -m pytest -q` (expect all green, exactly 2 known warnings).

```bash
git add app/config.py config.yaml tests/test_config.py
git commit -m "feat: add ai_actions_switch setting (master AI gate entity id)"
```

---

### Task 2: Master gate in the adapter

Enforce the switch before any ACTION-tier handler. Fail closed. First extract the handler-invocation into a helper (pure refactor, keeps the diff reviewable), then add the gate.

**Files:**
- Modify: `app/tools/adapter.py`
- Test: `tests/test_adapter.py`

**Interfaces:**
- Consumes: `Settings.ai_actions_switch` (Task 1); `ctx.rest.get_state(entity_id) -> dict` with a `"state"` key.
- Produces: module-level `async def _ai_gate_block(defn, ctx) -> ToolResult | None` (returns an error envelope to short-circuit, else `None`); module-level `async def _invoke_handler(defn, params_model, ctx, kwargs) -> ToolResult`.

- [ ] **Step 1: Extract `_invoke_handler` (refactor, no behavior change)**

In `app/tools/adapter.py`, move the existing `try: ... except Exception ...` suite that currently lives inside `_run`'s `else:` branch (the block that builds `params = params_model(**kwargs)`, calls `await defn.handler(...)`, and maps every exception to a `ToolResult.error(...)`) verbatim into a new module-level function placed just above `to_structured_tool`:

```python
async def _invoke_handler(defn, params_model, ctx, kwargs) -> ToolResult:
    try:
        params = params_model(**kwargs)
        return await defn.handler(params, ctx)
    except ValidationError as exc:
        return ToolResult.error("invalid_params", _validation_message(exc))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            entity_id = str(kwargs.get("entity_id", ""))
            data = (
                {"did_you_mean": await _did_you_mean(ctx, entity_id)}
                if entity_id
                else None
            )
            return ToolResult.error("entity_not_found", f"No entity {entity_id!r}.", data=data)
        return ToolResult.error(
            "ha_unreachable", f"Home Assistant returned HTTP {exc.response.status_code}.")
    except (httpx.HTTPError, ConnectionError) as exc:
        return ToolResult.error("ha_unreachable", f"Could not reach Home Assistant: {exc}.")
    except TimeoutError:
        return ToolResult.error("ha_timeout", "Home Assistant did not answer in time.")
    except PermissionError as exc:
        return ToolResult.error("domain_not_allowed", str(exc))
    except RuntimeError as exc:
        return ToolResult.error("ha_error", f"Command failed: {exc}.")
    except Exception as exc:  # terminal guard: nothing may escape into the agent loop
        log.exception("tool=%s unexpected error", defn.name)
        return ToolResult.error("internal_error", f"Unexpected error: {exc}.")
```

Then in `_run`, replace the `else:` branch body with a call to it:

```python
        if guard.is_repeat(thread_id, defn.name, args_json):
            result = ToolResult.error(
                "repeated_call",
                "You already called this tool with identical arguments. "
                "Use the previous result or try a different approach.",
            )
        else:
            result = await _invoke_handler(defn, params_model, ctx, kwargs)
```

- [ ] **Step 2: Run adapter tests to verify the refactor is green**

Run: `venv/bin/python -m pytest tests/test_adapter.py -q`
Expected: PASS (behavior unchanged).

- [ ] **Step 3: Write the failing gate tests**

Extend `tests/test_adapter.py`. First give the fake rest a `get_state` and let `_action_tool` configure the switch state; add these near the existing `_FakeRest` / `_action_tool` helpers:

```python
class _GateRest:
    """Fake rest whose get_state returns a configurable AI-switch state."""
    def __init__(self, switch_state="on", raise_on_get=False):
        self.switch_state = switch_state
        self.raise_on_get = raise_on_get

    async def list_states(self):
        return [{"entity_id": "light.kitchen"}]

    async def get_state(self, entity_id):
        if self.raise_on_get:
            raise httpx.ConnectError("refused")
        return {"entity_id": entity_id, "state": self.switch_state}


def _gate_tool(handler, rest, name="gate_demo", switch="input_boolean.ai_triggered_actions"):
    from app.config import Settings
    ctx = ToolContext(
        settings=Settings(_env_file=None, ai_actions_switch=switch), rest=rest, ws=None)
    defn = ToolDefinition(name=name, description="d", params_model=_Params,
                          tier=Tier.ACTION, handler=handler)
    return to_structured_tool(defn, ctx, LoopGuard())


async def test_action_allowed_when_ai_switch_on():
    ran = {"v": False}

    async def handler(params, ctx):
        ran["v"] = True
        return ToolResult.ok("done")

    tool = _gate_tool(handler, _GateRest(switch_state="on"))
    out = json.loads(await tool.ainvoke({"entity_id": "automation.ai_x"}))
    assert out["status"] == "ok"
    assert ran["v"] is True


async def test_action_blocked_when_ai_switch_off():
    ran = {"v": False}

    async def handler(params, ctx):
        ran["v"] = True
        return ToolResult.ok("done")

    tool = _gate_tool(handler, _GateRest(switch_state="off"))
    out = json.loads(await tool.ainvoke({"entity_id": "automation.ai_x"}))
    assert out["status"] == "error"
    assert out["error"]["code"] == "ai_disabled"
    assert ran["v"] is False  # handler never ran


async def test_action_blocked_when_switch_unavailable():
    async def handler(params, ctx):
        return ToolResult.ok("done")

    tool = _gate_tool(handler, _GateRest(switch_state="unavailable"))
    out = json.loads(await tool.ainvoke({"entity_id": "automation.ai_x"}))
    assert out["error"]["code"] == "ai_gate_unavailable"


async def test_action_blocked_when_switch_read_raises():
    async def handler(params, ctx):
        return ToolResult.ok("done")

    tool = _gate_tool(handler, _GateRest(raise_on_get=True))
    out = json.loads(await tool.ainvoke({"entity_id": "automation.ai_x"}))
    assert out["error"]["code"] == "ai_gate_unavailable"


async def test_gate_skipped_when_switch_setting_empty():
    async def handler(params, ctx):
        return ToolResult.ok("done")

    tool = _gate_tool(handler, _GateRest(switch_state="off"), switch="")
    out = json.loads(await tool.ainvoke({"entity_id": "automation.ai_x"}))
    assert out["status"] == "ok"  # gate disabled → off switch ignored


async def test_read_tier_bypasses_gate():
    async def handler(params, ctx):
        return ToolResult.ok("read")

    # tier READ tool: even a rest that raises on get_state must not be gated
    tool = _make_tool(handler, name="read_bypass")  # _FakeRest, tier READ
    out = json.loads(await tool.ainvoke({"entity_id": "light.kitchen"}))
    assert out["status"] == "ok"
```

- [ ] **Step 4: Run the new tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_adapter.py -q -k gate or switch or bypass`
Expected: the four block/allow gate tests FAIL (gate not implemented yet — actions currently just run).

- [ ] **Step 5: Implement `_ai_gate_block` and wire it in**

Add this module-level function above `_invoke_handler` in `app/tools/adapter.py`:

```python
async def _ai_gate_block(defn, ctx) -> ToolResult | None:
    """Master gate for ACTION-tier tools: refuse unless the AI-actions switch is on.
    Returns an error ToolResult to short-circuit, or None to proceed. Fails closed:
    any read problem or non-on state blocks the action."""
    if defn.tier < 2:
        return None
    switch = ctx.settings.ai_actions_switch
    if not switch:
        return None
    try:
        state = await ctx.rest.get_state(switch)
    except Exception:
        return ToolResult.error(
            "ai_gate_unavailable",
            f"Could not read the AI-actions switch {switch!r}; refusing to act.",
        )
    value = state.get("state")
    if value == "on":
        return None
    if value in ("unavailable", "unknown", None):
        return ToolResult.error(
            "ai_gate_unavailable",
            f"The AI-actions switch {switch!r} is {value!r}; refusing to act.",
        )
    return ToolResult.error(
        "ai_disabled",
        "AI-triggered actions are turned off at the home level. "
        "Enable the AI-actions switch to allow control.",
    )
```

Then wire it into `_run`'s `else:` branch (replacing the single `_invoke_handler` call from Step 1):

```python
        else:
            block = await _ai_gate_block(defn, ctx)
            if block is not None:
                result = block
            else:
                result = await _invoke_handler(defn, params_model, ctx, kwargs)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_adapter.py -q`
Expected: PASS — including the existing tier-2 audit tests (their fake rest now returns `state="on"` by default via `_GateRest`; the two pre-existing action tests use `_action_tool` with `_FakeRest`, so update `_FakeRest` to also expose `get_state` returning `{"state": "on"}` so those stay green):

In `_FakeRest` add:

```python
    async def get_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on"}
```

Re-run: `venv/bin/python -m pytest tests/test_adapter.py -q` → PASS.

- [ ] **Step 7: Verify the refusal is audited (defense-in-depth check)**

Confirm a gated refusal still flows through the tier≥2 audit block. Add:

```python
async def test_gated_refusal_is_audited():
    async def handler(params, ctx):
        return ToolResult.ok("done")

    sink = RecordingSink()
    from app.config import Settings
    ctx = ToolContext(
        settings=Settings(_env_file=None, ai_actions_switch="input_boolean.ai_triggered_actions"),
        rest=_GateRest(switch_state="off"), ws=None)
    ctx.audit = sink
    defn = ToolDefinition(name="act_gated", description="d", params_model=_Params,
                          tier=Tier.ACTION, handler=handler)
    tool = to_structured_tool(defn, ctx, LoopGuard())
    await tool.ainvoke({"entity_id": "automation.ai_x"},
                       config={"configurable": {"thread_id": "t"}})
    assert sink.rows[0]["status"] == "error"
    assert sink.rows[0]["error_code"] == "ai_disabled"
```

Run: `venv/bin/python -m pytest tests/test_adapter.py -q` → PASS.

- [ ] **Step 8: Commit**

```bash
git add app/tools/adapter.py tests/test_adapter.py
git commit -m "feat: master AI gate — refuse ACTION-tier tools unless ai_actions_switch is on"
```

---

### Task 3: Prefix-gate `trigger_automation`, drop its label check

`trigger_automation` becomes the menu enforcer. `control_entity` is still present and untouched here (its own label tests keep passing) — it is retired in Task 4.

**Files:**
- Modify: `app/tools/action/trigger_automation.py`
- Test: `tests/test_action_tools.py`

**Interfaces:**
- Produces: `AI_AUTOMATION_PREFIX = "automation.ai_"` (module-level constant, imported by Task 6).
- Error codes: `not_ai_controllable` (new), `invalid_params`, `domain_not_allowed` (unchanged).

- [ ] **Step 1: Update the trigger tests**

In `tests/test_action_tools.py`:

Change `test_trigger_automation_happy_path` to use an `ai_` automation:

```python
async def test_trigger_automation_happy_path():
    rest = FakeRest()
    rest.state = {"entity_id": "automation.ai_night", "state": "on",
                  "attributes": {"last_triggered": "2026-07-13T22:00:00+00:00"},
                  "last_changed": ""}
    defn = registry.get("trigger_automation")
    assert int(defn.tier) == 2
    result = await defn.handler(defn.params_model(entity_id="automation.ai_night"), _ctx(rest))
    assert result.status == "ok"
    assert rest.calls == [("automation", "trigger", "automation.ai_night")]
    assert result.data["last_triggered"] == "2026-07-13T22:00:00+00:00"
```

Change `test_trigger_automation_respects_allowlist` to use an `ai_` automation (so it passes the prefix check and reaches the domain check):

```python
async def test_trigger_automation_respects_allowlist():
    defn = registry.get("trigger_automation")
    result = await defn.handler(
        defn.params_model(entity_id="automation.ai_night"), _ctx(allowed=["light"]))
    assert result.error_code == "domain_not_allowed"
```

Add a new prefix-guard test:

```python
async def test_trigger_automation_rejects_non_ai_automation():
    rest = FakeRest()
    defn = registry.get("trigger_automation")
    result = await defn.handler(defn.params_model(entity_id="automation.night"), _ctx(rest))
    assert result.error_code == "not_ai_controllable"
    assert rest.calls == []  # never triggered
```

Delete the now-obsolete `test_trigger_automation_label_check_blocks` test entirely.

- [ ] **Step 2: Run the trigger tests to verify the new one fails**

Run: `venv/bin/python -m pytest tests/test_action_tools.py -q -k trigger`
Expected: `test_trigger_automation_rejects_non_ai_automation` FAILS (no prefix guard yet); the happy-path/allowlist edits may also fail until Step 3.

- [ ] **Step 3: Rewrite `trigger_automation.py`**

Replace the full contents of `app/tools/action/trigger_automation.py` with:

```python
from pydantic import BaseModel, Field

from app.tools.base import Tier, ToolDefinition, ToolResult
from app.tools.registry import register

# The AI menu marker. An automation is triggerable by the agent only when its
# entity_id starts with this prefix. Single source of truth (imported by
# get_automations for the ai_controllable flag).
AI_AUTOMATION_PREFIX = "automation.ai_"


class Params(BaseModel):
    entity_id: str = Field(
        description="AI-controllable automation entity id, e.g. 'automation.ai_night_lights'"
    )


async def handler(params: Params, ctx) -> ToolResult:
    if not params.entity_id.startswith("automation."):
        return ToolResult.error(
            "invalid_params", "entity_id must start with 'automation.'"
        )
    if not params.entity_id.startswith(AI_AUTOMATION_PREFIX):
        return ToolResult.error(
            "not_ai_controllable",
            f"{params.entity_id!r} is not an AI-controllable automation. "
            f"Only automations whose entity_id starts with {AI_AUTOMATION_PREFIX!r} "
            "may be triggered.",
        )
    if "automation" not in ctx.settings.allowed_domains:
        return ToolResult.error(
            "domain_not_allowed",
            "The 'automation' domain is not in the allowed list.",
            data={"allowed": list(ctx.settings.allowed_domains)},
        )
    await ctx.rest.call_service("automation", "trigger", params.entity_id)
    state = await ctx.rest.get_state(params.entity_id)
    return ToolResult.ok(
        {
            "entity_id": params.entity_id,
            "triggered": True,
            "last_triggered": state.get("attributes", {}).get("last_triggered"),
        }
    )


register(
    ToolDefinition(
        name="trigger_automation",
        description=(
            "Run an AI-controllable Home Assistant automation right now by its "
            "entity_id. Only automations whose entity_id starts with 'automation.ai_' "
            "may be triggered (see the ai_controllable flag from get_automations). "
            "Requires the home's AI-actions switch to be on."
        ),
        params_model=Params,
        tier=Tier.ACTION,
        handler=handler,
    )
)
```

- [ ] **Step 4: Run the trigger tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_action_tools.py -q -k trigger`
Expected: PASS. (Full-file green comes in Task 4 once `control_entity` and its label tests are removed.)

- [ ] **Step 5: Commit**

```bash
git add app/tools/action/trigger_automation.py tests/test_action_tools.py
git commit -m "feat: trigger_automation gates on automation.ai_ prefix, drops label check"
```

---

### Task 4: Retire `control_entity`

Delete the direct-control tool and update every consumer in one task — a reviewer can't accept the deletion while `tool_router`/`factory`/tests still name it.

**Files:**
- Delete: `app/tools/action/control_entity.py`
- Modify: `app/tools/registry.py`
- Modify: `app/agent/tool_router.py`
- Modify: `app/agent/factory.py`
- Test: `tests/test_action_tools.py`, `tests/test_tool_router.py`, `tests/test_factory.py`

**Interfaces:**
- Produces: `tools_for_tier(2)` yields exactly `{trigger_automation}`; tier-2 system prompt describes AI-automation triggering, not direct control.

- [ ] **Step 1: Update the failing tests first**

In `tests/test_factory.py`:

`test_build_agent_compiles_with_read_tools` — change the tier delta assertion:

```python
    assert tier2 - tier1 == {"trigger_automation"}
```

`test_system_prompt_mentions_control_only_at_tier2` — replace body to match the new tier-2 text:

```python
def test_system_prompt_mentions_control_only_at_tier2(tmp_path):
    s1 = Settings(_env_file=None, system_prompt="Base.", max_tier=1)
    s2 = Settings(_env_file=None, system_prompt="Base.", max_tier=2)
    p1 = build_system_prompt(s1, tmp_path)
    p2 = build_system_prompt(s2, tmp_path)
    assert "automation.ai_" not in p1  # no action guidance at tier 1
    assert "automation.ai_" in p2
    assert "trigger" in p2.lower()
```

`test_tool_subset_middleware_trims_to_message` — swap the sample tool name:

```python
    tools = [T("list_entities"), T("get_weather"), T("get_vacuum_state"), T("trigger_automation")]
```

In `tests/test_tool_router.py`:

- Remove `"control_entity",` from the `ALL_TOOLS` set.
- In `test_unrelated_query_hides_specialized_tools`, change the final assertion:

```python
    assert {"list_entities", "trigger_automation"} <= selected
```

In `tests/test_action_tools.py`, replace the whole file with the trigger-only version:

```python
import pytest

from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    def __init__(self):
        self.calls = []
        self.state = {"entity_id": "automation.ai_night", "state": "on",
                      "attributes": {"last_triggered": "2026-07-13T22:00:00+00:00"},
                      "last_changed": ""}

    async def call_service(self, domain, service, entity_id):
        self.calls.append((domain, service, entity_id))
        return []

    async def get_state(self, entity_id):
        return dict(self.state, entity_id=entity_id)


@pytest.fixture(autouse=True)
def load_tools():
    registry._reset_for_tests()
    registry.load_all(("app.tools.action.trigger_automation",))
    yield
    registry._reset_for_tests()


def _ctx(rest=None, allowed=None):
    settings = Settings(_env_file=None)
    if allowed is not None:
        settings.allowed_domains = allowed
    return ToolContext(settings=settings, rest=rest or FakeRest(), ws=None)


async def test_trigger_automation_happy_path():
    rest = FakeRest()
    defn = registry.get("trigger_automation")
    assert int(defn.tier) == 2
    result = await defn.handler(defn.params_model(entity_id="automation.ai_night"), _ctx(rest))
    assert result.status == "ok"
    assert rest.calls == [("automation", "trigger", "automation.ai_night")]
    assert result.data["last_triggered"] == "2026-07-13T22:00:00+00:00"


async def test_trigger_automation_rejects_non_automation_entity():
    defn = registry.get("trigger_automation")
    result = await defn.handler(defn.params_model(entity_id="light.kitchen"), _ctx())
    assert result.error_code == "invalid_params"


async def test_trigger_automation_rejects_non_ai_automation():
    rest = FakeRest()
    defn = registry.get("trigger_automation")
    result = await defn.handler(defn.params_model(entity_id="automation.night"), _ctx(rest))
    assert result.error_code == "not_ai_controllable"
    assert rest.calls == []


async def test_trigger_automation_respects_allowlist():
    defn = registry.get("trigger_automation")
    result = await defn.handler(
        defn.params_model(entity_id="automation.ai_night"), _ctx(allowed=["light"]))
    assert result.error_code == "domain_not_allowed"
```

- [ ] **Step 2: Run the updated tests to confirm they fail against current code**

Run: `venv/bin/python -m pytest tests/test_factory.py tests/test_tool_router.py tests/test_action_tools.py -q`
Expected: failures referencing `control_entity` still being present / old prompt text.

- [ ] **Step 3: Delete the tool and update the registry**

Delete the file:

```bash
git rm app/tools/action/control_entity.py
```

In `app/tools/registry.py`:
- Remove the line `    "app.tools.action.control_entity",` from `_DEFAULT_MODULES`.
- Update the module docstring (lines 4-6) so it no longer names `control_entity`:

```python
tools_for_tier() is the permission gate: the agent only sees tools at or
below max_tier. At max_tier=1 that means READ-only; at max_tier=2 the action
tool (trigger_automation) is also included.
```

- [ ] **Step 4: Update the tool router**

In `app/agent/tool_router.py`, remove `control_entity` from `CORE_TOOLS`:

```python
CORE_TOOLS: frozenset[str] = frozenset({
    "search_entities",
    "get_entity_state",
    "list_entities",
    "load_skill",
    "trigger_automation",
})
```

Update the comment above `CORE_TOOLS` to read "general-purpose querying + automation triggering" instead of "+ control".

- [ ] **Step 5: Update the tier-2 system prompt**

In `app/agent/factory.py`, in `build_system_prompt`, replace the `if settings.max_tier >= 2:` block with:

```python
    if settings.max_tier >= 2:
        prompt += (
            "\n\nACTIONS: you can trigger AI-controllable automations — those whose "
            "entity_id starts with 'automation.ai_' (see the ai_controllable flag from "
            "get_automations). You cannot control lights, switches, or other entities "
            "directly; act only by triggering one of these automations. Every action "
            "also requires the home's AI-actions switch to be on, or it is refused."
        )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_factory.py tests/test_tool_router.py tests/test_action_tools.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: retire control_entity — trigger_automation is the sole action tool"
```

---

### Task 5: Remove the label guardrail

With both action tools no longer using labels, delete the guardrail helper and the `allowed_labels` setting. `search_entities`'s own inline label *display* is independent and stays.

**Files:**
- Delete: `app/tools/helpers/labels.py`
- Modify: `app/config.py`
- Modify: `config.yaml`
- Modify: `CLAUDE.md` (helpers description)

**Interfaces:**
- Removes: `Settings.allowed_labels`; module `app.tools.helpers.labels`.

- [ ] **Step 1: Confirm no code imports the helper**

Run: `venv/bin/python -c "import app.tools.read.search_entities, app.tools.action.trigger_automation; print('ok')"`
Then verify nothing imports the labels helper:

```bash
command rg -n "helpers.labels|helpers/labels|allowed_labels" app tests
```

Expected: matches only in `app/config.py`, `config.yaml`, and `app/tools/helpers/labels.py` itself (no importers). If any test still references `allowed_labels`, it belongs to a file already rewritten in Task 4 — re-check.

- [ ] **Step 2: Delete the helper and the setting**

```bash
git rm app/tools/helpers/labels.py
```

In `app/config.py`, remove the line:

```python
    allowed_labels: list[str] = []  # if non-empty, entity/device must carry at least one
```

In `config.yaml`, remove `  allowed_labels: []` from `options:` and `  allowed_labels: ["str"]` from `schema:`.

- [ ] **Step 3: Update CLAUDE.md**

In `CLAUDE.md`, in the `tools/helpers/` bullet, remove the `labels.py (label guardrail)` clause so the description lists only the helpers that remain (`timerange.py`, `lookups.py`). If a nearby sentence describes the action safeguards as "tier gating + GET-only REST client (+ WS command allowlist ...)", leave it — it is still accurate.

- [ ] **Step 4: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: PASS, exactly 2 known warnings. A `ModuleNotFoundError` here means a consumer was missed — fix it before committing.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove label guardrail (allowed_labels + helpers/labels.py)"
```

---

### Task 6: Mark the menu in discovery (`ai_controllable`)

Give the agent a machine-readable menu marker on the automation listing.

**Files:**
- Modify: `app/tools/read/get_automations.py`
- Test: `tests/test_get_automations.py` (new)

**Interfaces:**
- Consumes: `AI_AUTOMATION_PREFIX` from `app.tools.action.trigger_automation` (Task 3).
- Produces: each list row from `get_automations` (no `entity_id` param) carries `ai_controllable: bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_get_automations.py`:

```python
from app.config import Settings
from app.tools import registry
from app.tools.context import ToolContext


class FakeRest:
    async def list_states(self):
        return [
            {"entity_id": "automation.ai_night_lights", "state": "on",
             "attributes": {"friendly_name": "AI: Night Lights"}},
            {"entity_id": "automation.morning", "state": "on",
             "attributes": {"friendly_name": "Morning"}},
            {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
        ]


def _ctx():
    return ToolContext(settings=Settings(_env_file=None), rest=FakeRest(), ws=None)


async def test_get_automations_marks_ai_controllable():
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.get_automations",))
    defn = registry.get("get_automations")
    result = await defn.handler(defn.params_model(entity_id=""), _ctx())
    registry._reset_for_tests()
    assert result.status == "ok"
    rows = {r["entity_id"]: r for r in result.data["rows"]}
    assert rows["automation.ai_night_lights"]["ai_controllable"] is True
    assert rows["automation.morning"]["ai_controllable"] is False
    assert "light.kitchen" not in rows  # non-automations excluded
```

Note: `bound_rows` (in `app/tools/base.py`) returns `{"rows": [...], "total": N}`, so the list branch of `get_automations` puts each row under `data["rows"]` — the assertion above is correct as written.

- [ ] **Step 2: Run the test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_get_automations.py -q`
Expected: FAIL — `KeyError: 'ai_controllable'`.

- [ ] **Step 3: Add the flag**

In `app/tools/read/get_automations.py`:

Add the import near the top:

```python
from app.tools.action.trigger_automation import AI_AUTOMATION_PREFIX
```

In the list branch (the `for a in autos:` loop that builds `rows`), add the field to each appended row:

```python
            rows.append({
                "entity_id": a["entity_id"],
                "state": a["state"],
                "name": a.get("attributes", {}).get("friendly_name", ""),
                "last_triggered": a.get("attributes", {}).get("last_triggered"),
                "ai_controllable": a["entity_id"].startswith(AI_AUTOMATION_PREFIX),
            })
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_get_automations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools/read/get_automations.py tests/test_get_automations.py
git commit -m "feat: mark ai_controllable automations in get_automations discovery"
```

---

### Task 7: Document the safety model and verify end-to-end

Record the new control model in CLAUDE.md and run the whole suite clean.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Document the control model**

In `CLAUDE.md`, under `## Hard rules`, add a bullet:

```markdown
- Writes are menu-only: the agent's only action tool is `trigger_automation`,
  which triggers only `automation.ai_*` automations, and every ACTION-tier call
  is gated in `tools/adapter.py` by a fail-closed point-read of
  `settings.ai_actions_switch` (default `input_boolean.ai_triggered_actions`).
  Off/unreadable ⇒ refused (`ai_disabled` / `ai_gate_unavailable`).
```

- [ ] **Step 2: Full verification**

Run: `venv/bin/python -m pytest -q`
Expected: all green. Confirm the warning summary shows exactly the 2 known third-party warnings — any additional warning is a finding to investigate before finishing.

- [ ] **Step 3: Confirm the tier-2 toolset and gate wiring by inspection**

Run:

```bash
venv/bin/python -c "
from app.tools import registry
registry.load_all()
names = sorted(t.name for t in registry.tools_for_tier(2))
delta = set(names) - set(t.name for t in registry.tools_for_tier(1))
print('tier2-only:', sorted(delta))
assert delta == {'trigger_automation'}, delta
print('OK')
"
```

Expected: `tier2-only: ['trigger_automation']` then `OK`.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record menu-only writes + AI gate in CLAUDE.md hard rules"
```

---

## SoT follow-up (separate repo — not part of this plan's test cycle)

In `/Users/ilniko/IdeaProjects/SmartHome/Suur-Ameerika`, author the real AI intents as `automation.ai_*` automations with clear `friendly_name`s, each individually on/off-toggleable. The `ai_test_*` fixtures already follow the `automation.ai_test_*` convention and back the eval cases — keep them. The harness gate is authoritative, so an in-automation `ai_triggered_actions` condition is optional; if added, trigger with `skip_condition: false` or it is a no-op.

## Self-Review

**Spec coverage:**
- Master gate (fail-closed point-read, `ai_disabled` / `ai_gate_unavailable`, empty-string bypass) → Task 2. ✓
- Retire `control_entity`, `trigger_automation` sole action tool → Task 4. ✓
- Prefix designation + `not_ai_controllable` → Task 3. ✓
- `ai_controllable` on discovery → Task 6. ✓
- Full label removal (helper incl. dead `entity_label_names`, `allowed_labels` in config + config.yaml) → Task 5; `search_entities` display kept (untouched by any task). ✓
- `ai_actions_switch` setting + config.yaml mirror → Task 1. ✓
- Error table codes (`ai_disabled`, `ai_gate_unavailable`, `not_ai_controllable`, `domain_not_allowed`, `invalid_params`) → Tasks 2, 3. ✓
- Testing matrix (gate on/off/unavailable/empty, prefix allow/deny, registry tier-2 set, label tests removed, evals resolve) → Tasks 2, 3, 4, 6, 7. ✓
- SoT contract → documented as follow-up (out of scope per spec). ✓

**Placeholder scan:** No TBD/TODO; every code step carries concrete code. The one conditional ("confirm `bound_rows` key") gives an explicit fallback instruction, not a placeholder.

**Type consistency:** `AI_AUTOMATION_PREFIX` defined in Task 3, imported in Task 6. `_ai_gate_block` / `_invoke_handler` signatures match between Task 2 definition and `_run` call sites. `ai_actions_switch` name identical across config.py, config.yaml, adapter, and tests. Error codes spelled identically across tasks and the spec's table.
