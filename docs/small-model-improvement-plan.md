# Small Model Improvement Plan

Goal: make the agent work reliably on sub-4B parameter models given hardware constraints.
Analysis based on eval runs of minicpm-ha (1B) and Qwen3.8-2B-GGUF:Q6_K (2B).

---

## What's actually broken

### Bug: JSON "Extra data" crashes the agent
Frequency: very common on 1B models.

After receiving a tool result, the model generates trailing text after the JSON tool call
object (e.g. `{...} here is my reasoning`). Python's JSON parser raises
`json.JSONDecodeError: Extra data at line 1 column X`. The agent crashes mid-turn.

`ContextWindowMiddleware` already handles `XML syntax error` the same way — catch and
return a retry message. It does not catch `json.JSONDecodeError`. Every
`[error] Extra data: line 1 column X` in the eval traces is this bug.

**Fix:** extend `ContextWindowMiddleware.awrap_model_call` in `app/agent/factory.py` to
also catch `json.JSONDecodeError` containing "Extra data", same recovery path as the XML
handler.

---

### Bug: eval entity IDs don't exist in real HA
Several eval cases use fictional entity IDs that are not in the real HA instance:
`light.kitchen`, `sensor.living_room_temperature`, `automation.night_lights`.

- Eval **passes** (correct tool routing on first call).
- `--verbose` run hits `entity_not_found`, the model loops through alternatives, and
  either hits the 15-step recursion limit or crashes on a JSON error.
- `history_needs_timestamps` and `history_range_today` both "pass" the score but spin
  to the recursion limit in real runs — the score is optimistic.

**Fix:** replace fictional entity IDs in `tests/evals/cases.yaml` with real ones from
the HA instance (`light.tapo_r1`, `automation.roborock_clean_rooms_when_no_one_s_home`,
etc.). Or mark routing-only cases and skip tracing for them.

---

### Tool confusion: `get_history` vs `get_logbook`
Both models consistently reach for `get_history` for "what happened in the house"
queries. `get_history` with `entity_id='*'` (wildcard) returns HTTP 400. The 2B model
recovers; the 1B model crashes on the JSON error that follows.

The descriptions don't draw a clear line between the two tools.

---

### Tool confusion: `get_areas` vs `list_entities`
For "what entities are in the Living room?" both models use `get_areas`, which returns
device names only — no entity IDs, no states. `list_entities(area=...)` is the correct
call. The descriptions don't distinguish these clearly.

---

### `range` param ambiguity in `get_history`
Both models send `range='last_24h'` when the user says "today". The `today` value
(since midnight local time) and `last_24h` (rolling 24-hour window) are not explained.

---

### Skill loading: wrong skill name
The 1B model guesses skill names (`'automation'` instead of `'diagnosing_automations'`).
Skill names only appear in the system prompt, not in the `load_skill` tool schema. The
model never sees the exact valid values at call time.

---

### Recursion loops on entity-not-found
When an entity doesn't exist, models keep retrying with varied searches rather than
reporting failure to the user. The loop guard only blocks identical repeated calls — it
doesn't stop varied searching toward the same missing entity.

---

## The plan

### 1. Fix "Extra data" crash in middleware
**File:** `app/agent/factory.py` — `ContextWindowMiddleware.awrap_model_call`

Extend the existing XML error handler:
```python
except Exception as exc:
    if "XML syntax error" not in str(exc) and "Extra data" not in str(exc):
        raise
    log.warning("LLM produced malformed tool call — retrying: %s", exc)
    return AIMessage(content="Your tool call contained malformed JSON or XML. Please retry.")
```

---

### 2. Rewrite `get_history` and `get_logbook` descriptions
**File:** `app/tools/query/get_history.py` and `app/tools/query/get_logbook.py`

**`get_history`:**
> Track a single entity's numeric or state value over time (temperature readings, power
> usage, battery %). Requires a specific entity_id — wildcards are not supported and will
> fail. NOT for "what happened in the house" or activity questions — use get_logbook for
> those.

**`get_logbook`:**
> What happened across the house — use for "what happened", "what events", "recent
> activity", "did X run" questions. Returns automations triggered, devices that changed
> state, and script executions across all or specific entities.

---

### 3. Clarify `range` param values
**File:** `app/tools/query/get_history.py` — `range` field description

Add: *"`today` = since midnight local time. `last_24h` = rolling 24-hour window. Use
`today` when the user says "today"; use `last_24h` when they say "last 24 hours".*

---

### 4. Rewrite `get_areas` vs `list_entities` descriptions
**File:** `app/tools/query/get_areas.py` and `app/tools/query/list_entities.py`

**`get_areas`:**
> Returns room names and device names only — no entity IDs, no states. Use to answer
> "what rooms do I have" or "show me the full map of my home". Do NOT use to find HA
> entity IDs or check entity states — use list_entities for that.

**`list_entities`:**
> Returns HA entities with entity_ids, states, and areas. Use when the user asks what
> is in a room and you need entity IDs to check or control them.

---

### 5. Embed skill names in `load_skill` schema
**File:** `app/tools/query/load_skill.py`

Change the `name` param from a plain `str` to `Literal["diagnosing_automations", ...]`
populated from the skills directory at import time. The model sees the exact valid values
in the tool schema and cannot hallucinate a name.

If a dynamic `Literal` is awkward, at minimum add the names to the field description:
*"Available skills: diagnosing_automations. Use the exact name."*

---

### 6. Guard `get_history` against wildcard `entity_id`
**File:** `app/tools/query/get_history.py` — handler

Return a structured error if `entity_id` is `'*'`, empty, or `'all'`:
```python
if params.entity_id in ("*", "", "all"):
    return ToolResult.error(
        "invalid_entity_id",
        "entity_id must be a specific entity. "
        "For cross-entity activity use get_logbook instead.",
    )
```

---

### 7. Fix eval entity IDs
**File:** `tests/evals/cases.yaml`

Replace:
| Current (fictional) | Replace with |
|---|---|
| `light.kitchen` | `light.tapo_r1` (or any real light) |
| `sensor.living_room_temperature` | a real sensor entity |
| `automation.night_lights` | `automation.presence_arrived_home` (or any real automation) |

This makes `--verbose` runs reflect genuine agent behavior rather than entity-not-found
loops.

---

### 8. Revert `logbook_clean_away_today` eval case
**File:** `tests/evals/cases.yaml`

Change back to `expect_tool: get_logbook`. The minicpm-ha verbose trace confirms
`get_logbook` gives a richer, more correct answer (exact timestamp + trigger chain).
The change to `get_automations` was based on one CLI session where `last_triggered` was
sufficient — `get_logbook` is the semantically correct tool for this query.

---

## Priority order

| # | Change | Type | Impact |
|---|---|---|---|
| 1 | JSON "Extra data" crash fix | Code | Unblocks 1B models entirely |
| 2 | `get_history` / `get_logbook` descriptions | Description | Fixes 2 failing cases on both models |
| 3 | `range` param doc | Description | Fixes `history_range_today` on both models |
| 4 | `get_areas` / `list_entities` descriptions | Description | Fixes `list_area` on 1B |
| 5 | Skill names in schema | Code/description | Fixes `skill_loading` on 1B |
| 6 | Wildcard guard in `get_history` | Code | Prevents HTTP 400 cascade |
| 7 | Fix eval entity IDs | Eval | Makes verbose scores meaningful |
| 8 | Revert `logbook_clean_away_today` | Eval | Fixes wrong expectation |
