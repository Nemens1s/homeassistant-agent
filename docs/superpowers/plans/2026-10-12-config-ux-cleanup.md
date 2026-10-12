# Config UX Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agent Gosling usable through configuration alone. That means fewer and clearer options, a prompt the code owns, tools that register themselves based on the home's entities, and skills supplied by the user.

**Architecture:** There are four phases, and each one ships as its own PR on its own branch. Phase 1 fixes the shipped config. Phase 2 reshapes `Settings` and `config.yaml`, and old option keys keep working through a `model_validator(mode="before")`. Phase 3 moves the system prompt into code and adds a middleware that attaches watched entities to the latest user turn. Phase 4 adds an async availability probe to each `ToolDefinition`, which decides which tools get registered.

**Tech Stack:** Python 3.14, pydantic-settings, langchain v1 (`create_agent`, `langchain.agents.middleware`), FastAPI, httpx, pytest (asyncio auto mode).

**Spec:** Claude Doc "Agent Gosling config UX review", https://claude.ai/code/artifact/efc46db4-b00c-4a73-b504-8555cf29ecce. The "Recommended direction" checklist and the comment threads together are the spec.

## Global Constraints

- Run everything through `uv run` (`uv run pytest -q`). Never use system python.
- The suite must keep exactly 1 warning (the starlette TestClient deprecation). Any new warning counts as a finding.
- Branch per phase: `config-ux/1-ship-fixes`, `config-ux/2-settings`, `config-ux/3-prompt`, `config-ux/4-tools`. Never commit to `main` and never use worktrees. Each phase ends with a PR.
- Bump the patch version in `config.yaml` once per phase PR.
- Every change a user can see (options, which tools are registered, skills, fast path) is documented in `README.md` in the same PR.
- Use plain, readable Python: `for` loops instead of comprehensions, explicit steps.
- Handlers never raise and never log errors themselves. Tool output is always `ToolResult...to_json()`.
- Writes stay menu-only: `automation.ai_*` and `script.ai_*`, gated by `ai_actions_switch`. Nothing in this plan widens that.
- Upgrading must not break: an existing `/data/options.json` that still holds removed or renamed keys has to load without errors. Unknown keys are ignored (`extra="ignore"`). Renamed keys map to their new names.
- Verify every langchain API against the installed venv (`uv run python -c "import inspect, ..."`). Don't trust memory for it.

## Review Focus

1. **Upgrade with an old options.json.** A home that has `max_tier: 2`, `allowed_domains`, `needle_*` and `system_prompt` must start with actions still enabled and the fast path still configured. A custom `system_prompt` should become `extra_instructions` and log a warning. Tested in Task 2.1.
2. **HA unreachable at startup.** The tool probe has to fail open and register every tool, because an add-on that comes up with no tools is worse than one that has a few useless ones. Tested in Task 4.1.
3. **More than 30 watched entities, or a state read that fails.** The middleware must leave the user turn untouched and never raise into the model call. Tested in Task 3.2.
4. **A home with two vacuums, or a vacuum with no room sensor.** `get_vacuum_state` has to work with the first vacuum or the override, and leave out `current_room`. Tested in Task 4.2.
5. **The skills folder is missing or contains a broken file.** `skills_enabled: true` with no folder means no skills and no `load_skill`, and must not crash. Tested in Task 4.3.

---

# Phase 1: Fix the shipped config (branch `config-ux/1-ship-fixes`)

### Task 1.1: Ship working write domains and drop dead options

**Files:**
- Modify: `config.yaml` (options, schema)
- Modify: `app/config.py` (remove `otlp_endpoint`, `telemetry_retention_days`, `needle_menu_ttl_s`)
- Modify: `app/main.py:76-95,193-194` (remove `_assert_otlp_is_lan` and its call)
- Modify: `app/needle/factory.py:31` (use a constant TTL)
- Modify: `tests/test_config.py`, `tests/test_main.py`, `tests/test_factory.py:160`, plus any test that references the removed fields
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `app/needle/factory.py` gets `_MENU_TTL_S = 60`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
def test_dead_options_are_gone():
    s = Settings(_env_file=None)
    assert not hasattr(s, "otlp_endpoint")
    assert not hasattr(s, "telemetry_retention_days")
    assert not hasattr(s, "needle_menu_ttl_s")


def test_old_options_json_with_dead_keys_still_loads(tmp_path, monkeypatch):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"otlp_endpoint": "", "telemetry_retention_days": 0,
                                "needle_menu_ttl_s": 60}))
    monkeypatch.setattr("app.config.OPTIONS_FILE", opts)
    load_settings()  # must not raise


def test_shipped_addon_options_allow_script():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    assert "script" in cfg["options"]["allowed_domains"]
    assert "automation" in cfg["options"]["allowed_domains"]
```

If `yaml` isn't importable, run `uv run python -c "import yaml"` to check. It is a transitive dependency of HA tooling. If it's missing, parse with `ruamel` or add `pyyaml` to the dev group.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_config.py -q`
Expected: 2 FAIL. The dead fields still exist and config.yaml lacks `script`.

- [ ] **Step 3: Implement**
  - In `config.yaml` `options:`, set `allowed_domains` to `[automation, script]`.
  - Delete `telemetry_retention_days` and `otlp_endpoint` from both `options:` and `schema:`, and delete `needle_menu_ttl_s` from both as well.
  - In `app/config.py`, delete the three fields and their comment lines.
  - In `app/main.py`, delete `_assert_otlp_is_lan` and the `if cfg.otlp_endpoint:` block.
  - In `app/needle/factory.py`, add `_MENU_TTL_S = 60  # menu/grammar cache TTL` and pass `ttl_s=_MENU_TTL_S`.
  - Fix any test that set those fields (`tests/test_factory.py:160` and the `needle_menu_ttl_s` assertion in `tests/test_config.py:55`).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass, 1 warning.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "fix(config): ship script in allowed_domains; drop dead options"
```

### Task 1.2: Remove this home's device names from tool text

**Files:**
- Modify: `app/tools/read/get_vacuum_state.py:36` (description only; the constants are replaced in Phase 4)
- Modify: `app/tools/read/list_devices.py:11,71`
- Modify: `app/agent/tool_router.py:55` (drop `"roborock"`)
- Test: `tests/test_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_registry.py
_HOME_SPECIFIC = ("roborock", "qrevo", "tallinn")


def test_no_home_specific_names_in_tool_text():
    registry._reset_for_tests()
    registry.load_all()
    offenders = []
    for t in registry.all_tools():
        text = (t.description + json.dumps(t.params_model.model_json_schema())).lower()
        for word in _HOME_SPECIFIC:
            if word in text:
                offenders.append((t.name, word))
    assert offenders == []
```

If `registry.all_tools()` doesn't exist, add it to `app/tools/registry.py`:

```python
def all_tools() -> list[ToolDefinition]:
    result = []
    for t in _REGISTRY.values():
        result.append(t)
    return result
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_registry.py -q`. Expected: FAIL, listing get_vacuum_state and list_devices.

- [ ] **Step 3: Implement**
  - In get_vacuum_state, change the description to `"Get the robot vacuum's CURRENT state and room. ..."`.
  - In list_devices, change the examples to `name='vacuum'` and `e.g. 'vacuum'`.
  - In tool_router, remove `"roborock"`.

- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.

- [ ] **Step 5: Commit and open PR 1**

```bash
git add -A && git commit -m "fix(tools): remove home-specific names from tool text"
git push -u origin config-ux/1-ship-fixes
gh pr create --title "Config UX phase 1: shipped config fixes" --body "..."
```

---

# Phase 2: Settings reshape (branch `config-ux/2-settings`, from main after PR 1)

### Task 2.1: `allow_actions` replaces `max_tier`; `allowed_domains` is gone; old keys still load

**Files:**
- Modify: `app/constants.py` (add `WRITE_DOMAINS`)
- Modify: `app/config.py`
- Modify: `app/main.py:166-170`, `app/cli.py:72-76`, `tests/evals/run.py:82` (use `WRITE_DOMAINS`)
- Modify: `app/tools/helpers/actions.py:29-34` (delete the domain check, which the prefix check above it already covers)
- Modify: `app/tools/action/notify_user.py:37-38` (delete the check)
- Modify: `app/tools/read/search_entities.py:64-67` (use `WRITE_DOMAINS`)
- Modify: tests that pass `allowed_domains`: `test_action_checks.py:36`, `test_notify_user.py:100`, `test_action_tools.py:38`, `test_main.py:91`, `test_config.py:70`. Delete them or update them to the new behaviour.
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.allow_actions: bool`.
- Produces: the read-only property `Settings.max_tier -> int`, which returns 2 if `allow_actions` is on and 1 otherwise. Every existing `settings.max_tier` call site keeps working unchanged.
- Produces: `app.constants.WRITE_DOMAINS = ("automation", "script")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
def test_allow_actions_default_off():
    s = Settings(_env_file=None)
    assert s.allow_actions is False
    assert s.max_tier == 1


def test_allow_actions_maps_to_tier_2():
    assert Settings(_env_file=None, allow_actions=True).max_tier == 2


def test_legacy_max_tier_key_still_works():
    assert Settings(_env_file=None, max_tier=2).allow_actions is True
    assert Settings(_env_file=None, max_tier=1).allow_actions is False


def test_allow_actions_wins_over_legacy_max_tier():
    assert Settings(_env_file=None, allow_actions=False, max_tier=2).allow_actions is False


def test_allowed_domains_is_gone_and_ignored():
    s = Settings(_env_file=None, allowed_domains=["light"])
    assert not hasattr(s, "allowed_domains")
```

- [ ] **Step 2: Run and confirm failures.** Run: `uv run pytest tests/test_config.py -q`

- [ ] **Step 3: Implement in `app/config.py`**

```python
from pydantic import model_validator

    # Agent behavior
    allow_actions: bool = False  # off = read-only; on = trigger_action/notify_user/scheduling

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data):
        # Old options.json files carry max_tier (1|2). Map it unless the new key is set.
        if isinstance(data, dict) and "max_tier" in data:
            data = dict(data)
            legacy = data.pop("max_tier")
            if "allow_actions" not in data:
                data["allow_actions"] = int(legacy) >= 2
        return data

    @property
    def max_tier(self) -> int:
        # Tier is an internal concept (tools/base.Tier); users only see allow_actions.
        if self.allow_actions:
            return 2
        return 1
```

Remove the `allowed_domains` field and the `max_tier` field. Then wire everything through `WRITE_DOMAINS`:
- In main.py, cli.py and evals, `write_domains = WRITE_DOMAINS if cfg.allow_actions else ()`.
- In search_entities, loop over `WRITE_DOMAINS` and skip `automation`, as the current loop does.

Tests that assign `settings.max_tier = 2` to an existing object (`tests/test_main.py:85,235`) must change to `settings.allow_actions = True`, because the property can't be assigned. Tests that build `Settings(max_tier=2)` work unchanged through the validator.

- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass, 1 warning.

- [ ] **Step 5: Update `config.yaml` and commit.**
  - Options: replace `max_tier: 1` with `allow_actions: false` and remove `allowed_domains`.
  - Schema: add `allow_actions: bool` and remove `max_tier` and `allowed_domains`.

```bash
git add -A && git commit -m "feat(config): allow_actions replaces max_tier; drop allowed_domains"
```

### Task 2.2: Rename needle to fast path, with legacy keys still read

**Files:**
- Modify: `app/config.py` (rename three fields, using `AliasChoices` for the old names)
- Modify: `app/needle/factory.py`, `app/agent/middleware/__init__.py:42`, `app/cli.py:110`, `config.yaml`
- Modify: `tests/test_config.py:51-56`, `tests/test_main.py:236-237`, `tests/conftest.py` (env list)
- Modify: `README.md` (new "Fast path" section)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.fast_path_enabled: bool`, `Settings.fast_path_url: str`, `Settings.fast_path_confidence_threshold: float`.
- Produces: the old keys `needle_enabled`, `needle_remote_url` and `needle_confidence_threshold` are accepted as aliases.

The `app/needle/` package name stays for now. Renaming the module isn't something users see, and it would bloat the diff.

- [ ] **Step 1: Write the failing test**

```python
def test_fast_path_keys_and_legacy_needle_aliases():
    s = Settings(_env_file=None, fast_path_enabled=True, fast_path_url="http://x")
    assert s.fast_path_enabled and s.fast_path_url == "http://x"
    legacy = Settings(_env_file=None, needle_enabled=True, needle_remote_url="http://y",
                      needle_confidence_threshold=0.4)
    assert legacy.fast_path_enabled is True
    assert legacy.fast_path_url == "http://y"
    assert legacy.fast_path_confidence_threshold == 0.4
```

- [ ] **Step 2: Run and confirm it fails.**

- [ ] **Step 3: Implement**

```python
    # Fast path (optional): an external service that maps a request straight to
    # one ai_* action, skipping the LLM. Contract: README "Fast path".
    fast_path_enabled: bool = Field(
        False, validation_alias=AliasChoices("fast_path_enabled", "needle_enabled"))
    fast_path_url: str = Field(
        "", validation_alias=AliasChoices("fast_path_url", "needle_remote_url"))
    # Fire only at or above this confidence. Keep 0 unless your model reports a
    # calibrated confidence (e.g. a model fine-tuned on Needle's site).
    fast_path_confidence_threshold: float = Field(
        0.0, validation_alias=AliasChoices("fast_path_confidence_threshold",
                                           "needle_confidence_threshold"))
```

`AliasChoices` disables the field name as an input unless the field name is listed, which is why it is listed first. Also check whether env-var input still works: run `uv run python -c "from app.config import Settings; import os; os.environ['FAST_PATH_ENABLED']='true'; print(Settings(_env_file=None).fast_path_enabled)"`. If it prints False, add `populate_by_name=True` to `model_config`.

Update the consumers: needle/factory.py (log messages say "fast path"), middleware `__init__` (`settings.fast_path_confidence_threshold`), cli.py (`[fast path ON]`), and config.yaml. Add the three new env names to conftest's `_SETTINGS_ENV_VARS`.

- [ ] **Step 4: Write the README "Fast path" section.** It needs:
  - The request and response JSON contract. Copy it from `app/needle/remote_backend.py`, and read that file first so the contract is exact.
  - The `FAST-PATH:` description marker convention, taken from `app/constants.py` `FAST_PATH_MARKERS`.
  - A sample automation:

```yaml
alias: "AI: start vacuum"
id: ai_action_start_vacuum
description: "FAST-PATH: start the vacuum / clean the house"
triggers: []
actions:
  - action: vacuum.start
    target:
      entity_id: vacuum.my_vacuum
```

  Also replace the stale README rows for `max_tier` and `allowed_domains` (lines 35-42) with `allow_actions`.

- [ ] **Step 5: Run** `uv run pytest -q`, **then commit**:

```bash
git add -A && git commit -m "feat(config): rename needle options to fast_path; document contract"
```

### Task 2.3: Auto-detect the LLM provider and read the llama.cpp context size

**Files:**
- Create: `app/agent/llm_probe.py`
- Modify: `app/config.py` (`llm_provider` default `"auto"`)
- Modify: `app/main.py` lifespan (before `build_agent`), `app/cli.py` (before `build_agent`)
- Test: `tests/test_llm_probe.py`

**Interfaces:**
- Produces: `async def resolve_llm_settings(cfg: Settings, client: httpx.AsyncClient | None = None) -> Settings`. It returns a `model_copy` with `llm_provider` resolved and, for llamacpp, `num_ctx` taken from `/props`. It never raises.

- [ ] **Step 1: Write the failing tests**, using `httpx.MockTransport`:

```python
import httpx
from app.agent.llm_probe import resolve_llm_settings
from app.config import Settings


def _client(routes):
    def handler(request):
        fn = routes.get(request.url.path)
        if fn is None:
            return httpx.Response(404)
        return fn(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://llm")


async def test_detects_ollama():
    c = _client({"/api/tags": lambda r: httpx.Response(200, json={"models": []})})
    s = await resolve_llm_settings(Settings(_env_file=None, llm_url="http://llm"), c)
    assert s.llm_provider == "ollama"


async def test_detects_llamacpp_and_reads_n_ctx():
    c = _client({"/props": lambda r: httpx.Response(
        200, json={"default_generation_settings": {"n_ctx": 32768}})})
    s = await resolve_llm_settings(Settings(_env_file=None, llm_url="http://llm"), c)
    assert s.llm_provider == "llamacpp"
    assert s.num_ctx == 32768


async def test_api_key_means_cloud_without_probing():
    c = _client({})
    s = await resolve_llm_settings(Settings(_env_file=None, api_key="k"), c)
    assert s.llm_provider == "openai"


async def test_explicit_provider_is_kept():
    c = _client({"/api/tags": lambda r: httpx.Response(200, json={})})
    s = await resolve_llm_settings(Settings(_env_file=None, llm_provider="llamacpp"), c)
    assert s.llm_provider == "llamacpp"


async def test_unreachable_falls_back_to_openai_compat():
    def boom(request):
        raise httpx.ConnectError("down")
    c = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://llm")
    s = await resolve_llm_settings(Settings(_env_file=None), c)
    assert s.llm_provider == "openai"
```

Check the `/props` shape against the llama-server version in use. Run `curl http://<llm>/props` on the real server before writing the parser. Newer builds put `n_ctx` under `default_generation_settings`. If it sits elsewhere, adjust the test to match the real response.

- [ ] **Step 2: Run and confirm they fail** (module missing).

- [ ] **Step 3: Implement `app/agent/llm_probe.py`**

```python
"""Works out llm_provider (and llama.cpp's real context size) from the endpoint,
so users only give a URL and a model. Never raises: on any problem it falls
back to the plain OpenAI-compatible client."""

from __future__ import annotations

import logging

import httpx

from app.config import Settings

log = logging.getLogger("agent.llm")

_PROBE_TIMEOUT = 3.0


async def _get_json(client: httpx.AsyncClient, path: str):
    try:
        response = await client.get(path, timeout=_PROBE_TIMEOUT)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _n_ctx(props: dict) -> int | None:
    settings = props.get("default_generation_settings") or {}
    value = settings.get("n_ctx") or props.get("n_ctx")
    if isinstance(value, int) and value > 0:
        return value
    return None


async def resolve_llm_settings(cfg: Settings, client: httpx.AsyncClient | None = None) -> Settings:
    if cfg.llm_provider != "auto":
        return cfg
    if cfg.api_key:
        return cfg.model_copy(update={"llm_provider": "openai"})
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(base_url=cfg.llm_url)
    try:
        props = await _get_json(client, "/props")
        if isinstance(props, dict):
            update = {"llm_provider": "llamacpp"}
            n_ctx = _n_ctx(props)
            if n_ctx is not None:
                update["num_ctx"] = n_ctx
            log.info("llm probe: llama.cpp detected (n_ctx=%s)", n_ctx)
            return cfg.model_copy(update=update)
        tags = await _get_json(client, "/api/tags")
        if isinstance(tags, dict):
            log.info("llm probe: Ollama detected")
            return cfg.model_copy(update={"llm_provider": "ollama"})
        log.warning("llm probe: could not identify %s; using OpenAI-compatible client", cfg.llm_url)
        return cfg.model_copy(update={"llm_provider": "openai"})
    finally:
        if own_client:
            await client.aclose()
```

In `app/config.py`, set `llm_provider: str = "auto"`. In the main.py lifespan, put `cfg = await resolve_llm_settings(cfg)` before `ToolContext(...)` is built. `cfg` is a closure variable from `create_app`, so rebind a local `run_cfg` and use it everywhere below that point. In cli.py, do the same before `build_agent`. Update `tests/test_config.py::test_defaults` so it expects `"auto"`.

- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(llm): auto-detect provider and llama.cpp n_ctx"
```

### Task 2.4: Shrink the visible add-on options

**Files:**
- Modify: `config.yaml`, `docs/DEPLOYMENT.md`
- Test: `tests/test_config.py`

The visible `options:` keep exactly these defaults:
- `llm_url: ""`, `llm_model: ""`, `api_key: ""`
- `allow_actions: false`, `ai_actions_switch: "input_boolean.ai_triggered_actions"`, `notify_action: "script.ai_action_notify"`
- `watched_entities: []`, `clock_entity: ""`, `event_confirmations_enabled: true`
- `person_name_map: []`
- `language_layer_enabled: false`, `languages: [en]`

`extra_instructions` comes in Phase 3. Every other key stays in `schema:` but gets a `?` and is removed from `options:`, so the Supervisor hides it until set. Paths (`checkpoint_db_path`, `telemetry_db_path`, `glossary_path`) leave the schema completely: `run.sh` exports them as env vars, the way `AUDIT_DB_PATH` already works.

- [ ] **Step 1: Write the failing test**

```python
def test_visible_addon_options_are_minimal():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    assert set(cfg["options"]) == {
        "llm_url", "llm_model", "api_key", "allow_actions", "ai_actions_switch",
        "notify_action", "watched_entities", "clock_entity",
        "event_confirmations_enabled", "person_name_map",
        "language_layer_enabled", "languages",
    }
    for key in cfg["schema"]:
        if key not in cfg["options"]:
            assert str(cfg["schema"][key]).endswith("?") or isinstance(cfg["schema"][key], list), key


def test_every_schema_key_is_a_settings_field():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    fields = set(Settings.model_fields)
    for key in cfg["schema"]:
        assert key in fields, key
```

- [ ] **Step 2: Run and confirm it fails.**

- [ ] **Step 3: Rewrite `config.yaml` `options:`/`schema:`** as described above. In `run.sh`, add:

```bash
export CHECKPOINT_DB_PATH=/data/checkpoints.sqlite
export TELEMETRY_DB_PATH=/data/telemetry.sqlite
export GLOSSARY_PATH=/config/gosling/glossary.yaml
```

Add `max_history_messages` to the hidden schema with code default 40. That means changing the `Settings` default from 0 to 40, because the add-on default was 40. `clock_entity` gets an empty code default. Then update `test_defaults`, and any test that relied on the `sensor.europe_tallinn` default, so it sets the value explicitly.

Note: options.json is written by the Supervisor, and env vars also feed `Settings`. Check that env plus options.json merge correctly. `load_settings` passes the options as init kwargs, which take priority over env, so a path absent from options.json comes from env. Write a test for that:

```python
def test_paths_come_from_env_when_absent_from_options(tmp_path, monkeypatch):
    opts = tmp_path / "options.json"
    opts.write_text("{}")
    monkeypatch.setattr("app.config.OPTIONS_FILE", opts)
    monkeypatch.setenv("CHECKPOINT_DB_PATH", "/data/checkpoints.sqlite")
    assert load_settings().checkpoint_db_path == "/data/checkpoints.sqlite"
```

- [ ] **Step 4: Run** `uv run pytest -q`, **update DEPLOYMENT.md's options table, commit, open PR 2.**

```bash
git add -A && git commit -m "feat(addon): minimal visible options; advanced ones optional"
```

---

# Phase 3: The code owns the prompt (branch `config-ux/3-prompt`)

### Task 3.1: `extra_instructions` replaces `system_prompt`; the prompt is shown in the UI

**Files:**
- Modify: `app/config.py` (`DEFAULT_SYSTEM_PROMPT` becomes `_BASE_PROMPT` in factory.py; add the `extra_instructions` field)
- Modify: `app/agent/factory.py:62-93`
- Modify: `app/main.py` (add `GET /api/prompt`)
- Modify: `frontend/metrics.html` (a "System prompt" panel)
- Modify: `config.yaml` (add `extra_instructions: ""` to options and `extra_instructions: "str?"` to schema), and update the Task 2.4 test's key set
- Test: `tests/test_factory.py`, `tests/test_main.py`

**Interfaces:**
- Produces: `build_system_prompt(settings, skills_dir) -> str`, same signature as before.
- Produces: `Settings.extra_instructions: str`.
- Produces: `GET /api/prompt`, which returns `{"prompt": str}`.

The new `_BASE_PROMPT` is the config.yaml prompt with the sentences about actions and scheduling taken out, because those sections are added conditionally:

```python
_BASE_PROMPT = (
    "You are an assistant for this Home Assistant instance. "
    "Answer questions about the home using the available tools, and always "
    "look up real data with tools instead of guessing. "
    "Once a tool returns an answer, do not overthink it — present the "
    "information as you found it. "
    "Be concise and factual, follow instructions precisely, and do not do "
    "anything extra that wasn't asked."
)
```

- [ ] **Step 1: Write the failing tests**

```python
def test_prompt_starts_with_code_base_and_appends_extra(tmp_path):
    s = Settings(_env_file=None, extra_instructions="Call me Captain.")
    prompt = build_system_prompt(s, tmp_path)
    assert prompt.startswith("You are an assistant for this Home Assistant instance.")
    assert prompt.rstrip().endswith("Call me Captain.")


def test_no_rule_is_stated_twice(tmp_path):
    s = Settings(_env_file=None, allow_actions=True, watched_entities=["vacuum.x"])
    prompt = build_system_prompt(s, tmp_path)
    assert prompt.count("never tell the user to create an automation") <= 1
    assert prompt.count("first call list_actions") == 1


def test_read_only_prompt_does_not_mention_triggering(tmp_path):
    prompt = build_system_prompt(Settings(_env_file=None), tmp_path)
    assert "trigger_action" not in prompt


def test_legacy_system_prompt_becomes_extra_instructions(caplog):
    s = Settings(_env_file=None, system_prompt="Old custom text.")
    assert s.extra_instructions == "Old custom text."
    assert "system_prompt is deprecated" in caplog.text
```

The case-insensitive "never tell the user to create an automation" count can come out as 2 today, because ACTIONS and DOING THINGS LATER both say it. Rewrite ACTIONS so it only points at DOING THINGS LATER, and drop its own copy of the sentence.

- [ ] **Step 2: Run and confirm they fail.**

- [ ] **Step 3: Implement.** In `Settings._legacy_keys`, also map `system_prompt` to `extra_instructions` when the latter is absent. Log the warning with `logging.getLogger("config").warning("system_prompt is deprecated; using it as extra_instructions")`. Skip that mapping when the value equals the old shipped default, so it isn't duplicated. Compare after `" ".join(value.split())` normalisation against the old config.yaml text, kept as a module constant `_OLD_SHIPPED_PROMPT`.

In `build_system_prompt`, start from `_BASE_PROMPT + _TOOL_ROUTING`, keep the conditional blocks, and finish with:

```python
    extra = settings.extra_instructions.strip()
    if extra:
        prompt += "\n\nHOUSEHOLD INSTRUCTIONS:\n" + extra
    return prompt
```

Endpoint in main.py, next to `/api/health`:

```python
    @app.get("/api/prompt")
    async def get_prompt():
        from app.agent.factory import build_system_prompt
        return {"prompt": build_system_prompt(app.state.settings, app.state.ctx.skills_dir)}
```

The lifespan sets `app.state.ctx = ctx`. In `frontend/metrics.html`, add a collapsible `<details><summary>System prompt</summary><pre id="prompt"></pre></details>` filled by `fetch("api/prompt")`. Use a relative URL, because ingress serves the app under a path prefix.

- [ ] **Step 4: Run** `uv run pytest -q`, then `uv run python -m tests.evals.run` against live Ollama/llama.cpp, once before the change (on main) and once after. Record both pass rates in the PR description. A drop of more than one case blocks the PR.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(prompt): code-owned prompt + extra_instructions; /api/prompt view"
```

### Task 3.2: Attach watched entities to the latest user turn

**Files:**
- Create: `app/agent/middleware/watched_entities_middleware.py`
- Modify: `app/agent/middleware/__init__.py` (insert after `ToolSubsetMiddleware` so routing sees the raw user text)
- Test: `tests/test_watched_entities_middleware.py`

**Interfaces:**
- Consumes: `ctx.rest.get_state(entity_id) -> dict`, `Settings.watched_entities`.
- Produces: `WatchedEntitiesMiddleware(rest, entity_ids: list[str], cap: int = 30, ttl_s: float = 20.0)`.

**Behavior:** for each model call, copy the latest `HumanMessage` and append a block like this to its text:

```
<home_context>
Watched devices (use these entity_ids directly; no need to search):
- vacuum.x "Vacuum" = docked
</home_context>
```

Only the request copy changes. The checkpointed state never does. Because the block is added fresh to the latest turn on every call, older turns never carry it, so nothing has to be stripped. If there are more than `cap` ids, or there are none, the request passes through unchanged. A state that fails to read is listed without ` = <state>`. Results are cached for `ttl_s` so a multi-step run reads HA only once.

- [ ] **Step 1: Write the failing tests**

```python
from dataclasses import dataclass, field
from langchain_core.messages import AIMessage, HumanMessage
from app.agent.middleware.watched_entities_middleware import WatchedEntitiesMiddleware


class FakeRest:
    def __init__(self, states, fail=()):
        self.states, self.fail, self.calls = states, set(fail), 0

    async def get_state(self, eid):
        self.calls += 1
        if eid in self.fail:
            raise RuntimeError("boom")
        return {"entity_id": eid, "state": self.states[eid],
                "attributes": {"friendly_name": eid.split(".")[1].title()}}


@dataclass
class Req:
    messages: list
    tools: list = field(default_factory=list)


async def _run(mw, req):
    seen = {}

    async def handler(r):
        seen["req"] = r
        return AIMessage(content="ok")

    await mw.awrap_model_call(req, handler)
    return seen["req"]


async def test_appends_block_to_latest_human_only():
    rest = FakeRest({"vacuum.x": "docked"})
    mw = WatchedEntitiesMiddleware(rest, ["vacuum.x"])
    old = HumanMessage("earlier")
    latest = HumanMessage("start cleaning when I leave")
    out = await _run(mw, Req([old, AIMessage("hi"), latest]))
    assert out.messages[0].content == "earlier"
    assert "<home_context>" in out.messages[-1].content
    assert 'vacuum.x "X" = docked' in out.messages[-1].content
    assert latest.content == "start cleaning when I leave"  # original untouched


async def test_over_cap_passes_through():
    ids = []
    for i in range(31):
        ids.append(f"sensor.s{i}")
    mw = WatchedEntitiesMiddleware(FakeRest({}), ids, cap=30)
    out = await _run(mw, Req([HumanMessage("hi")]))
    assert out.messages[-1].content == "hi"


async def test_failed_state_read_still_lists_entity():
    mw = WatchedEntitiesMiddleware(FakeRest({}, fail=["vacuum.x"]), ["vacuum.x"])
    out = await _run(mw, Req([HumanMessage("hi")]))
    assert "- vacuum.x\n" in out.messages[-1].content


async def test_states_cached_within_ttl():
    rest = FakeRest({"vacuum.x": "docked"})
    mw = WatchedEntitiesMiddleware(rest, ["vacuum.x"], ttl_s=60)
    await _run(mw, Req([HumanMessage("a")]))
    await _run(mw, Req([HumanMessage("b")]))
    assert rest.calls == 1
```

- [ ] **Step 2: Run and confirm they fail.**

- [ ] **Step 3: Implement.** Check that `request.messages` and `replace(request, messages=...)` work by inspecting `ModelRequest` in the venv (`uv run python -c "from langchain.agents.middleware import ModelRequest; import inspect; print(inspect.signature(ModelRequest))"`), following the `ContextWindowMiddleware._prepare` pattern. The sync `wrap_model_call` passes through unchanged, because the agent is always driven async. Confirm that with `grep -n "invoke\|stream" app/main.py app/cli.py app/events/runner.py`, which should show only `ainvoke`/`astream`. If a sync path exists, note it in the PR.

```python
class WatchedEntitiesMiddleware(AgentMiddleware):
    """Puts the user's watched entities (id, name, state) on the latest user turn
    for this model call only, so the model skips search round-trips. The
    checkpointed messages are never changed."""

    def __init__(self, rest, entity_ids, cap=30, ttl_s=20.0):
        super().__init__()
        self._rest = rest
        self._ids = list(entity_ids)
        self._cap = cap
        self._ttl_s = ttl_s
        self._cached_block = None
        self._cached_at = 0.0

    async def _block(self) -> str:
        now = time.monotonic()
        if self._cached_block is not None and now - self._cached_at < self._ttl_s:
            return self._cached_block
        lines = ["<home_context>",
                 "Watched devices (use these entity_ids directly; no need to search):"]
        for eid in self._ids:
            try:
                s = await self._rest.get_state(eid)
            except Exception:
                lines.append(f"- {eid}")
                continue
            name = s.get("attributes", {}).get("friendly_name", "")
            lines.append(f'- {eid} "{name}" = {s.get("state", "")}')
        lines.append("</home_context>")
        self._cached_block = "\n".join(lines)
        self._cached_at = now
        return self._cached_block

    async def awrap_model_call(self, request, handler):
        if not self._ids or len(self._ids) > self._cap:
            return await handler(request)
        index = None
        for i in range(len(request.messages) - 1, -1, -1):
            if getattr(request.messages[i], "type", None) == "human":
                index = i
                break
        if index is None or not isinstance(request.messages[index].content, str):
            return await handler(request)
        block = await self._block()
        messages = list(request.messages)
        original = messages[index]
        messages[index] = original.model_copy(update={"content": original.content + "\n\n" + block})
        try:
            prepared = replace(request, messages=messages)
        except TypeError:  # pragma: no cover
            prepared = request.override(messages=messages)
        return await handler(prepared)

    def wrap_model_call(self, request, handler):
        return handler(request)
```

Wire it in `build_middleware`. It needs `rest`, so add a `rest=None` parameter, passed from `build_agent` as `ctx.rest`. Append it after `ContextWindowMiddleware` so trimming runs first. ToolSubset sits at index 0 and sees the request before this middleware changes it. Add a test that `build_middleware` places it after `ToolSubsetMiddleware`.

**Telemetry check:** the telemetry middleware records `input_text` from the request. Confirm it reads the checkpointed or HTTP input and not the augmented message: `grep -n "input_text" app/telemetry/*.py app/agent/middleware/telemetry_middleware.py`. If it reads the augmented message, strip `<home_context>` there.

- [ ] **Step 4: Run** `uv run pytest -q`, then the evals with `WATCHED_ENTITIES` set to a real vacuum. Expect fewer `search_entities` calls on scheduling cases.

- [ ] **Step 5: Commit, open PR 3.**

```bash
git add -A && git commit -m "feat(agent): attach watched entities to latest user turn"
```

---

# Phase 4: Tools that register themselves, and user-supplied skills (branch `config-ux/4-tools`)

### Task 4.1: Availability probe plus `disabled_tools`

**Files:**
- Modify: `app/tools/base.py` (add an `available` field)
- Modify: `app/tools/registry.py` (add `async def resolve_enabled(ctx) -> set[str]`)
- Modify: `app/tools/adapter.py:187` (`build_tools(..., enabled: set[str] | None = None)`)
- Modify: `app/agent/factory.py` (`build_agent(..., enabled=None)` passes it to `build_tools`)
- Modify: `app/main.py`, `app/cli.py`, `tests/evals/run.py` (compute `enabled` before `build_agent`)
- Modify: `app/config.py` (add `disabled_tools: list[str] = []`), `config.yaml` (add `disabled_tools: ["str?"]` to the schema)
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `ToolDefinition.available: Callable[[Any], Awaitable[bool]] | None = None`. `None` means always available.
- Produces: `registry.resolve_enabled(ctx) -> set[str]`, the names allowed by tier that aren't in `settings.disabled_tools` and whose probe returned True. If a probe raises, its tool is kept (fail open), and a warning is logged.
- Produces: `build_tools(ctx, max_tier, guard=None, enabled=None)`. When `enabled` is None, all tools are built, which is the current behaviour.

Helper tools are the ones that have a probe: `get_vacuum_state`, `get_battery_status`, `get_weather`, `get_person_locations`. Every other tool can still be switched off by name in `disabled_tools`. The core action tools (`trigger_action`, `list_actions`, `notify_user`) can be disabled too, and the result is simply that the agent can't do that thing.

- [ ] **Step 1: Write the failing tests**

```python
async def _true(ctx):
    return True


async def _false(ctx):
    return False


async def _raises(ctx):
    raise RuntimeError("HA down")


def _defn(name, available=None):
    async def h(p, c):
        return ToolResult.ok(None)
    return ToolDefinition(name=name, description="d", params_model=BaseModel,
                          tier=Tier.READ, handler=h, available=available)


async def test_resolve_enabled_applies_probe_and_disabled_list():
    registry._reset_for_tests()
    registry.register(_defn("always"))
    registry.register(_defn("has_device", _true))
    registry.register(_defn("no_device", _false))
    registry.register(_defn("probe_fails", _raises))
    registry.register(_defn("user_off", _true))
    ctx = ToolContext(settings=Settings(_env_file=None, disabled_tools=["user_off"]), rest=None)
    assert await registry.resolve_enabled(ctx) == {"always", "has_device", "probe_fails"}


def test_build_tools_respects_enabled():
    registry._reset_for_tests()
    registry.register(_defn("a"))
    registry.register(_defn("b"))
    ctx = ToolContext(settings=Settings(_env_file=None), rest=None)
    names = []
    for t in build_tools(ctx, max_tier=1, enabled={"a"}):
        names.append(t.name)
    assert names == ["a"]
```

- [ ] **Step 2: Run and confirm they fail.**

- [ ] **Step 3: Implement**

```python
# registry.py
log = logging.getLogger("agent.tools")


async def resolve_enabled(ctx) -> set[str]:
    """Tool names to offer in this home: allowed by tier, not switched off by
    the user, and (for helper tools) backed by a matching entity. A probe that
    fails keeps its tool — better a useless tool than a missing one."""
    disabled = set(ctx.settings.disabled_tools)
    enabled = set()
    for t in tools_for_tier(ctx.settings.max_tier):
        if t.name in disabled:
            continue
        if t.available is None:
            enabled.add(t.name)
            continue
        try:
            ok = await t.available(ctx)
        except Exception as exc:
            log.warning("tool=%s availability probe failed (%s); keeping it", t.name, exc)
            ok = True
        if ok:
            enabled.add(t.name)
        else:
            log.info("tool=%s not registered: nothing in this home to use it on", t.name)
    return enabled
```

In `build_tools`, skip `d` when `enabled is not None and d.name not in enabled`. In main.py, call `enabled = await registry.resolve_enabled(ctx)` after `registry.load_all()` and pass `enabled=enabled` to `build_agent`, and also to the `build_tools` call at main.py:265, so the toolset snapshot matches. Do the same in cli.py and in evals.

- [ ] **Step 4: Run** `uv run pytest -q`. **Commit.**

```bash
git add -A && git commit -m "feat(tools): availability probes + disabled_tools"
```

### Task 4.2: Probes for the helper tools; `get_vacuum_state` finds its own entities

**Files:**
- Create: `app/tools/helpers/probes.py`
- Modify: `app/tools/read/get_vacuum_state.py` (full rewrite), `get_battery_status.py`, `get_weather.py`, `get_person_locations.py` (add `available=` only)
- Modify: `app/config.py` (add `vacuum_entity: str = ""`), `config.yaml` (add `vacuum_entity: "str?"`)
- Modify: `tests/test_get_vacuum_state.py`
- Test: `tests/test_probes.py`, `tests/test_get_vacuum_state.py`

**Interfaces:**
- Produces: `async def any_entity(ctx, prefix: str = "", device_class: str = "") -> bool` in `probes.py`. It reads `ctx.rest.list_states()` once and returns True if any state matches.
- Produces: `async def find_vacuum(ctx) -> str | None`, which returns `settings.vacuum_entity` if set, otherwise the first `vacuum.*` sorted by entity_id.
- Produces: `async def room_sensor_for(ctx, vacuum_id: str) -> str | None`. It needs `ws_ready`. It finds the vacuum's `device_id` in `config/entity_registry/list` and returns the first sibling `sensor.*` on the same device whose entity_id ends with `_current_room` or whose `translation_key` is `current_room`. Before relying on the `translation_key` branch, check the real registry entry with `mcp__homeassistant__ha_get_entity` on `sensor.roborock_qrevo_s_current_room`.

- [ ] **Step 1: Write the failing tests** (`tests/test_probes.py`)

```python
class Rest:
    def __init__(self, states):
        self._states = states

    async def list_states(self):
        return self._states


class WS:
    connected = True

    def __init__(self, entities):
        self._entities = entities

    async def request_cached(self, cmd):
        assert cmd == "config/entity_registry/list"
        return self._entities


def _ctx(states, ws=None, **settings):
    return ToolContext(settings=Settings(_env_file=None, **settings), rest=Rest(states), ws=ws)


async def test_any_entity_by_prefix_and_device_class():
    states = [{"entity_id": "sensor.b", "attributes": {"device_class": "battery"}}]
    assert await any_entity(_ctx(states), device_class="battery") is True
    assert await any_entity(_ctx(states), prefix="vacuum.") is False


async def test_find_vacuum_override_then_first():
    states = [{"entity_id": "vacuum.z", "attributes": {}}, {"entity_id": "vacuum.a", "attributes": {}}]
    assert await find_vacuum(_ctx(states)) == "vacuum.a"
    assert await find_vacuum(_ctx(states, vacuum_entity="vacuum.z")) == "vacuum.z"
    assert await find_vacuum(_ctx([])) is None


async def test_room_sensor_found_via_same_device():
    ents = [
        {"entity_id": "vacuum.a", "device_id": "d1"},
        {"entity_id": "sensor.a_battery", "device_id": "d1"},
        {"entity_id": "sensor.a_current_room", "device_id": "d1"},
        {"entity_id": "sensor.other_current_room", "device_id": "d2"},
    ]
    assert await room_sensor_for(_ctx([], ws=WS(ents)), "vacuum.a") == "sensor.a_current_room"


async def test_room_sensor_none_without_ws():
    assert await room_sensor_for(_ctx([], ws=None), "vacuum.a") is None
```

Check how `ws_ready` decides readiness (`app/ha/websocket.py` `ws_is_ready`) and make the fake `WS` satisfy it. Copy the fake from `tests/test_ws_ready.py` rather than guessing the attribute.

Rewrite `tests/test_get_vacuum_state.py` so it uses `vacuum.a` / `sensor.a_current_room` through `FakeRest.list_states` plus the WS fake, and keep the five existing scenarios. Add one more:

```python
async def test_no_vacuum_returns_error_envelope():
    result = await _defn().handler(_defn().params_model(), _ctx_with_states([]))
    assert result.status == "error"
    assert result.error_code == "no_vacuum"
```

- [ ] **Step 2: Run and confirm they fail.**

- [ ] **Step 3: Implement `probes.py`.** In `get_vacuum_state.py`, delete `_VACUUM_ENTITY` and `_ROOM_SENSOR`. The handler calls `find_vacuum`. If it gets None, it returns `ToolResult.error("no_vacuum", "No vacuum entity found in Home Assistant.")`. Otherwise it reads the vacuum state and, when `room_sensor_for` returns an id, reads that sensor with the existing unknown/unavailable filtering. Register with `available=_has_vacuum`, where:

```python
async def _has_vacuum(ctx) -> bool:
    return await find_vacuum(ctx) is not None
```

Add `available=` to the other helper tools:
- battery: `lambda ctx: any_entity(ctx, device_class="battery")`
- weather: `prefix="weather."`
- person: `prefix="person."`

Write each one as a small named `async def` rather than a lambda, in keeping with the readability rule.

- [ ] **Step 4: Document helper tools in `README.md`.** Add a "Tools" section that covers:
  - The core tools and what each needs. Copy the "Register when" column from the review doc's Tools table.
  - The helper tools and their trigger entities:

    | Tool | Registered when |
    |---|---|
    | `get_vacuum_state` | a `vacuum.*` entity exists |
    | `get_battery_status` | an entity with `device_class: battery` exists |
    | `get_weather` | a `weather.*` entity exists |
    | `get_person_locations` | a `person.*` entity exists |

  - The checks run once at add-on start. After adding a device, restart the add-on.
  - If Home Assistant can't be reached at startup, every tool is registered. A tool with nothing behind it then returns a clear error rather than disappearing.
  - `disabled_tools: [get_weather, ...]` turns off any tool by name. This includes the core tools, with the obvious result that the agent then can't do that thing.
  - `get_vacuum_state` uses the first `vacuum.*` (sorted by entity_id) and finds its room sensor on the same device. Set `vacuum_entity` to pick one when there are several. The room is left out when no `_current_room` sensor exists.
  - A short log hint: each skipped tool is logged once at startup as `tool=<name> not registered: nothing in this home to use it on`.

- [ ] **Step 5: Run** `uv run pytest -q`. **Commit.**

```bash
git add -A && git commit -m "feat(tools): helper tools auto-register; vacuum discovered not hard-coded"
```

### Task 4.3: Skills are user-supplied and off by default

**Files:**
- Move: `app/skills/*.md` to `docs/examples/skills/`, using `git mv`, so users have something to copy
- Modify: `app/tools/context.py` (`skills_dir` default comes from settings)
- Modify: `app/config.py` (add `skills_enabled: bool = False` and `skills_path: str = "/config/gosling/skills"`), `config.yaml` (add `skills_enabled: "bool?"` to the schema)
- Modify: `app/tools/read/load_skill.py` (`available=` probe: `skills_enabled` is on and at least one skill parses)
- Delete: `app/tools/read/list_skills.py` and its line in `_DEFAULT_MODULES`; delete the related tests in `tests/test_skills.py`
- Modify: `app/agent/factory.py` (skills block only when `settings.skills_enabled`)
- Modify: `app/agent/tool_router.py` (`load_skill` stays in CORE; nothing changes, since a tool that isn't registered is never offered)
- Modify: `app/agent/factory.py` `_TOOL_ROUTING` (remove "diagnosing why one failed → load_skill." and add that hint inside the skills block instead)
- Test: `tests/test_skills.py`, `tests/test_factory.py`, `tests/test_load_skill.py`

**Interfaces:**
- Consumes: `app.skills.list_skills(path) -> list[SkillMeta]` (existing).
- Produces: `ToolContext.skills_dir` defaults to `Path(settings.skills_path)`. Build it in `__post_init__` when it isn't passed explicitly, and keep the explicit override for tests.

- [ ] **Step 1: Write the failing tests**

```python
def test_skills_block_absent_when_disabled(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\nbody")
    prompt = build_system_prompt(Settings(_env_file=None), tmp_path)
    assert "SKILL PLAYBOOKS" not in prompt
    assert "load_skill" not in prompt


def test_skills_block_present_when_enabled(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\nbody")
    prompt = build_system_prompt(Settings(_env_file=None, skills_enabled=True), tmp_path)
    assert "- a: d" in prompt


async def test_load_skill_unavailable_when_folder_missing(tmp_path):
    registry._reset_for_tests()
    registry.load_all(("app.tools.read.load_skill",))
    ctx = ToolContext(settings=Settings(_env_file=None, skills_enabled=True), rest=None,
                      skills_dir=tmp_path / "missing")
    assert "load_skill" not in await registry.resolve_enabled(ctx)


async def test_broken_skill_file_is_skipped(tmp_path):
    (tmp_path / "bad.md").write_text("no frontmatter at all")
    (tmp_path / "good.md").write_text("---\nname: good\ndescription: d\n---\nbody")
    names = []
    for m in list_skills(tmp_path):
        names.append(m.name)
    assert names == ["good"]


def test_no_skills_ship_in_image():
    from pathlib import Path
    assert list(Path("app/skills").glob("*.md")) == []
```

Before writing the folder-missing case, run `uv run python -c "from app.skills import list_skills; from pathlib import Path; print(list_skills(Path('/nope')))"`. If it raises, make `list_skills` return `[]` for a missing folder.

- [ ] **Step 2: Run and confirm they fail.**

- [ ] **Step 3: Implement** as listed under Files. In the README, add a short "Skills (optional)" section: drop markdown files with `name`/`description` frontmatter into `/config/gosling/skills/`, set `skills_enabled: true`, and see `docs/examples/skills/`. Skills are prompt text only and can't widen actions beyond `automation.ai_*`/`script.ai_*`.

- [ ] **Step 4: Run** `uv run pytest -q`, then the evals. Diagnosis cases that expect `load_skill` must be skipped when skills are off: add `"requires_skills": true` to those cases and skip them in `tests/evals/run.py` when `not settings.skills_enabled`.

- [ ] **Step 5: Commit, update CLAUDE.md** (the `skills/` bullet now points to `/config/gosling/skills`, and the "Adding a tool" note mentions `available=`), **and open PR 4.**

```bash
git add -A && git commit -m "feat(skills): user-supplied skills, off by default; drop list_skills"
```

---

## Out of scope (recorded so it isn't lost)

- **Tying tool routing to the language layer.** Translation already runs at the HTTP edge before the agent (`i18n.inbound`), so routing already sees English when the layer is on. For a multilingual model without the layer, the answer is to set `enable_tool_subsetting: false`, which is an advanced option. Phase 2 documents that in DEPLOYMENT.md and adds no code.
- **A `clock_entity` fallback to HA's time zone plus the add-on's system clock.** This needs its own small design: time maths in `events/clock.py` assumes the World Clock's wall time.
- **Renaming the `app/needle/` package.** The rename isn't visible to users and isn't worth the diff right now.
