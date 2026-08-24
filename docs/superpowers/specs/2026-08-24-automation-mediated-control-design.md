# Automation-mediated control with a home-level AI gate

**Date:** 2026-08-24
**Status:** Design — approved for planning
**Scope:** `homeassistant-ollama-agent` (agent repo). The SmartHome SoT repo
authors the automations against the contract described here; that authoring is
out of scope for the agent-repo implementation plan but its interface is
specified below.

## Problem

Iteration 2 gave the agent two ACTION-tier tools: `control_entity` (direct
light/switch on/off/toggle) and `trigger_automation`. Both are gated by
`allowed_domains` plus an optional `allowed_labels` guardrail. Direct control
lets the LLM compose arbitrary service calls against any allowed entity — the
blast radius is "every light and switch," and the only global off-switch is
editing config.

We want two things instead:

1. **A curated menu, not arbitrary control.** The agent should act only through
   automations the user has authored and reviewed in their git SoT. The LLM
   picks from a fixed menu of named intents; it cannot invent service calls.
2. **A home-level master switch.** A single Home Assistant boolean
   (`input_boolean.ai_triggered_actions`, already present on the user's Home
   Settings dashboard) decides whether the AI may act at all. Off ⇒ no writes.

This extends the project's "safety is structural, not a runtime flag" stance:
today reads are guaranteed by a GET-only client and tier gating; after this
change, writes are guaranteed to be menu-only (prefix-gated automations) and
globally gated by a fail-closed point-read of the master switch.

## Decisions (locked)

- **Control model:** fixed menu. `control_entity` is retired. `trigger_automation`
  becomes the only ACTION-tier tool.
- **Master gate:** point-read of the switch at action time, enforced in the
  adapter seam. No state subscription (that is deferred to iteration 3, where a
  proactive use case justifies the machinery; a gate must read at decision time
  to avoid stale-cache races).
- **Menu designation:** naming prefix only — `automation.ai_*`. Labels are
  dropped entirely (see rationale below).
- **Fail closed:** if the switch cannot be confirmed `on`, the action is refused.

### Why prefix, not labels

Labels were considered as the designation marker and rejected:

- **Out of SoT.** Automation labels live in HA's entity registry (`.storage`),
  not in the automation YAML. A label-based marker is invisible to the user's
  git SoT — un-reviewable in a PR, un-greppable, drift-prone. The prefix is in
  the `entity_id`: version-controlled and self-documenting.
- **Technically weaker.** The prefix is a pure string check on `entity_id`:
  always available, deterministic, fail-closed. The label check goes through the
  WS registry and returns `None` when it cannot resolve (e.g. `ctx.ws is None`
  degraded mode), which the current code treats as *not-False* → **allowed**.
  The label guard fails *open* in degraded mode; the prefix never does.
- **No migration benefit here.** Labels' one real advantage — tagging a
  pre-existing automation without renaming — does not apply, because these are
  dedicated, purpose-built intents whose `entity_id`s are chosen up front.

With labels no longer serving as a guardrail, they are removed from the agent
entirely, except for the independent read-only label *display* in
`search_entities` (kept — it is informational and unrelated to the guardrail).

## Architecture

### 1. Master gate (adapter seam)

`app/tools/adapter.py` is the single seam where cross-cutting behavior lives.
The `_run` wrapper already has an `if defn.tier >= 2:` block that runs *after*
the handler to emit the action audit log. We add a **pre-execution** gate for
tier ≥ 2 tools, before `defn.handler` is invoked:

1. Read the switch entity named by `settings.ai_actions_switch`
   (default `input_boolean.ai_triggered_actions`).
2. If its state is exactly `"on"` → proceed to the handler.
3. Otherwise short-circuit — the handler never runs — with a `ToolResult.error`:
   - state is any non-`on` value (e.g. `off`) → `error_code="ai_disabled"`,
     message explaining the AI-actions switch is off.
   - state cannot be read (HTTP/connection error, entity missing/unavailable)
     → `error_code="ai_gate_unavailable"`. **Fail closed.**
4. If `settings.ai_actions_switch` is the empty string, the gate is disabled
   entirely (for tests and local dev). This is the only bypass.

The refusal envelope flows through the existing tier ≥ 2 audit block at the
bottom of `_run` (`status=error`), so "the AI attempted an action while
disabled" becomes a first-class audit record with the correct `error_code`.

**Ordering inside `_run`:** loop-guard repeat check → gate check → handler.
The gate read happens inside the existing `try/except` so any transport
exception maps cleanly; the gate itself distinguishes `ai_disabled` from
`ai_gate_unavailable` rather than leaking a generic `ha_unreachable`.

One enforcement point covers every present and future ACTION-tier tool.

### 2. `trigger_automation` becomes prefix-gated

`app/tools/action/trigger_automation.py`:

- Remove the `check_entity_labels` block.
- Keep the `entity_id.startswith("automation.")` validation.
- Add: the automation must match the AI menu prefix `automation.ai_`. Anything
  else is refused with `error_code="not_ai_controllable"` and a message telling
  the LLM it may only trigger AI-controllable automations.
- Keep the existing `allowed_domains` check for the `automation` domain.

The prefix is a module-level constant so the same value is shared with
discovery (below).

### 3. `control_entity` retired

- Remove `app.tools.action.control_entity` from `_DEFAULT_MODULES` in
  `app/tools/registry.py`.
- Delete `app/tools/action/control_entity.py`.
- Update the registry module docstring (it currently names `control_entity` as
  an example ACTION tool).
- Net effect: `tools_for_tier(2)` yields exactly `{trigger_automation}`.

### 4. Discovery marks the menu

`app/tools/read/get_automations.py` (READ tier — still lists *all* automations
for general Q&A) gains one computed field per row in the list branch:

- `ai_controllable: bool` — `True` when `entity_id` starts with the
  `automation.ai_` prefix.

This is the LLM's menu marker. No new tool, no breaking change, no extra HTTP
call (prefix is derived from the `entity_id` already present). Intent wording
is carried by each automation's `friendly_name`, which is already a state
attribute in the listing — no per-automation config fetches. The system prompt
(or a skill) instructs the agent that it may only trigger `ai_controllable`
automations.

### 5. Label guardrail removed

- Delete `app/tools/helpers/labels.py` in full — both `check_entity_labels`
  (used only by the two action tools) and `entity_label_names` (dead code,
  imported nowhere).
- Remove `allowed_labels` from `app/config.py`.
- Remove both `allowed_labels` entries from `config.yaml` (options schema and
  example/defaults).
- **Kept:** `search_entities`'s own inline `_label_map` and the `row["labels"]`
  read-side display. It is independent of the guardrail and purely
  informational.

### 6. Config

`app/config.py` `Settings`:

- Add `ai_actions_switch: str = "input_boolean.ai_triggered_actions"`.
  Empty string disables the gate.
- Remove `allowed_labels`.
- `allowed_domains` stays. Only `automation` is meaningful on the write path now;
  `light`/`switch` no longer appear in any action tool.

`config.yaml` (addon options — must mirror `Settings` field names exactly):

- Add `ai_actions_switch` to the options schema and defaults.
- Remove `allowed_labels`.

The addon container reads `/data/options.json`; the new key must match the
`Settings` field name.

## SoT contract (SmartHome repo — interface only)

AI-facing automations authored in the user's SmartHome SoT:

- **entity_id:** `automation.ai_*` (this prefix is the guardrail and the
  discovery marker).
- **friendly_name:** clear, human-and-LLM readable — this is the intent label
  the agent sees in discovery.
- **mode:** as appropriate per intent.
- **Individually disableable:** native automation on/off is the per-capability
  kill switch.
- **Master gate is the harness**, so an in-automation `ai_triggered_actions`
  condition is *optional* defense-in-depth. If added, note that
  `automation.trigger` skips conditions by default (`skip_condition: true`) —
  the condition only takes effect when triggered with `skip_condition: false`,
  so it must not be relied upon as the primary gate.
- The existing `ai_test_*` fixtures graduate into this convention and remain the
  eval fixtures they already are.

## Error handling

| Condition | `error_code` | Where |
|-----------|--------------|-------|
| Master switch is `off` (or any non-`on` state) | `ai_disabled` | adapter gate |
| Master switch unreadable / missing / unavailable | `ai_gate_unavailable` | adapter gate |
| Automation not matching `automation.ai_` prefix | `not_ai_controllable` | `trigger_automation` |
| `automation` domain not allowed | `domain_not_allowed` | `trigger_automation` |
| `entity_id` not starting `automation.` | `invalid_params` | `trigger_automation` |

All refusals are `ToolResult.error(...)` envelopes serialized via `to_json()` —
never raw exceptions — and tier ≥ 2 refusals are audited.

## Testing

`venv/bin/python -m pytest -q` — no HA/Ollama needed; exactly two known
third-party warnings expected, any new warning is a finding.

- **Gate:** tier ≥ 2 tool refused when switch `off` (`ai_disabled`); refused
  when switch read raises / entity missing (`ai_gate_unavailable`, fail-closed);
  allowed when switch `on`; gate skipped when `ai_actions_switch=""`.
- **`trigger_automation`:** allows `automation.ai_foo`; refuses
  `automation.plain` with `not_ai_controllable`; still refuses non-`automation.`
  ids with `invalid_params`.
- **`get_automations`:** list rows carry correct `ai_controllable` values for
  prefixed vs non-prefixed automations.
- **Registry:** `tools_for_tier(2)` is exactly `{trigger_automation}`;
  `control_entity` is gone.
- **Label removal:** obsolete label tests in `tests/test_action_tools.py`
  deleted; no import of `app.tools.helpers.labels` remains; `search_entities`
  label-display tests (if any) still pass.
- **Evals:** `tests/evals` cases referencing `ai_test_*` still resolve (these
  need live Ollama and are not collected by pytest).

## Out of scope

- WS state subscription for the gate (deferred to iteration 3).
- Parameterized intents (automations triggered via `automation.trigger` do not
  receive arbitrary variables; the fixed menu is unparameterized named intents).
- Re-adding an optional label check for exposing legacy automations without
  renaming — a one-liner if ever needed, not built now.
- Authoring the real AI automations in the SmartHome SoT repo (separate repo;
  contract specified above).
