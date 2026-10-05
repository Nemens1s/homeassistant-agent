---
repo: homeassistant-agent
date: 2026-10-05
status: draft (questions settled in chat, spec awaiting review)
builds-on: 2026-10-04-event-memory-notes-design.md
---

# Direct scheduled actions: design

## Context

Scheduled tasks (`schedule_on_state_change`, `schedule_at_time`) currently fire into a fresh LLM run on the `events` thread. That run re-reads the instruction, calls `list_actions` and picks the script again. This causes three problems:

- **Reliability:** a 2B model gets a second chance to get it wrong, and nothing checks the result.
- **Latency:** the vacuum keeps moving while the model thinks.
- **Opacity:** a task shows `fired` whether or not anything happened.

The model already makes the right choice when the user is talking to it. This design saves that choice when the task is scheduled, and replays it in Python when the trigger happens.

### Success criteria

- "Once the vacuum starts, stop it" stores `script.ai_action_stop_vacuum` on the task. When the vacuum starts cleaning, the script runs within about a second, with **no LLM call**.
- A wrong or unknown action is rejected **while scheduling**, with the valid choices, so the model corrects itself in the same conversation.
- "Stop the vacuum and remind me to tidy up" is **one** task. It runs the action and sends the reminder.
- Every fired task records an outcome (`done` / `failed` / `no_action`) that is visible on the Notes page. A failed action notifies the user even when confirmations are off.

## Decisions (settled 2026-10-05)

| # | Question | Decision |
|---|---|---|
| 1 | How is the confirmation worded without an LLM? | A fixed Python sentence, translated through the existing notify path. |
| 2 | Can one task hold both an action and a reminder? | Yes, both on one task. |
| 3 | How many actions per task? | One. Anything more complex falls back to the LLM run. |
| 4 | Do existing tasks need migrating? | No. Nothing is deployed yet. A local DB only needs the new columns added on start. |

## The task model

A task no longer has a single `kind`. It carries up to two payloads:

| Field | Meaning |
|---|---|
| `action_entity_id` + `action_params` | One `ai_*` action to run directly. Optional. |
| `reminder` | Text to send to the user. Optional. |
| `instruction` | A human-readable description of the task, always present. Used for display and for the LLM fallback. |

These combinations give four ways a task can fire:

| action | reminder | When the trigger happens |
|---|---|---|
| ✓ | – | Run the action directly. Send the confirmation unless suppressed. |
| – | ✓ | Send the reminder. It is always delivered. |
| ✓ | ✓ | Run the action, then send one notification combining the confirmation and the reminder. The reminder part is always sent. |
| – | – | **LLM fallback**: today's `[EVENT]` run with the instruction. Used for things no single action covers. |

`kind` disappears from the tool parameters. The store's `kind` column is derived (`action` / `reminder` / `both` / `agent`) and is kept only for display.

## Tool changes (`schedule_on_state_change`, `schedule_at_time`)

New optional parameters, replacing `kind`:
- `action_entity_id: str`. An action from `list_actions`, the same thing the model would pass to `trigger_action`.
- `action_params: dict`. Arguments for that script.
- `reminder: str`. A short message for the user, in English.

Checks when the task is saved. Each failure returns an error envelope, and nothing is saved:
1. `action_entity_id`, when given, must be an AI-controllable action: an `automation.ai_*` or `script.ai_*` in `allowed_domains`, and not `notify_action`. Otherwise `not_ai_controllable` / `use_notify_user`, with the list of valid actions from the `list_actions` logic.
2. For a script, `action_params` are checked against its fields using `trigger_action`'s existing `_validate_script_params`. Otherwise `invalid_params`.
3. `reminder` is normalised to English with `to_english`, like `instruction`.

The descriptions gain one line: "If a single action from list_actions does it, pass action_entity_id (and action_params); if the user wants to be told something, pass reminder." The router adds `list_actions` alongside the scheduling tools, since it is already CORE.

## Firing (`app/events/runner.py`)

The listener is unchanged: SQL match, AI-switch gate, mark fired. It then calls the runner, which now dispatches per task:

```
for each task in the batch (oldest first):
    if task.action_entity_id:
        result = run the action through the SAME path as trigger_action
    if task.reminder or (action and confirmations wanted) or action failed:
        build one fixed sentence and send it through the notify_user path
    if neither action nor reminder:
        queue for the LLM fallback
if any queued: one [EVENT] LLM run with just those tasks (today's behaviour)
record each task's outcome
```

**Same gates, no new write path.** Direct actions and notifications run through `to_structured_tool(trigger_action)` and `to_structured_tool(notify_user)`, called with the `events` configurable and the run scope. That gives them:
- the AI-switch gate (a second check after the listener's),
- the audit row and the `agent.actions` log,
- the loop guard,
- the error-to-envelope mapping,
- for notifications: translation and suppression.

Python never calls `rest.call_service` directly.

**Fixed sentences** (English; `notify_user` translates them into the user's language):

| Situation | Text |
|---|---|
| Action ok | `Done: {action name}.` |
| Action failed | `Couldn't run {action name}: {error message}.` |
| Reminder | `Reminder: {reminder}` |
| Both | The action sentence and the reminder sentence joined with a space. |

`{action name}` is the action's friendly name from HA, never a raw id, matching the fast path's rule (commit f4f2fc7).

**Suppression** (`event_confirmations_enabled: false`) now applies to **one sentence**: the "Done: …" confirmation.
- A reminder is never suppressed.
- A failure is never suppressed.
- When a notification contains only a suppressed confirmation, it is not sent. `notify_user` still returns its usual "sent" result.

## Outcomes (`memory_notes`)

New columns, added on start with `ALTER TABLE … ADD COLUMN` when missing (decision 4):

| Column | Values |
|---|---|
| `action_entity_id` | text, nullable |
| `action_params` | JSON text, default `{}` |
| `reminder` | text, nullable |
| `outcome` | `done` / `failed` / `no_action` / `agent`, set when the task fires |
| `result` | a short human-readable line, e.g. `Stop Vacuum ✓` or the error message |

- `outcome = agent` means the task went to the LLM fallback. Its result is taken from whether that run called `trigger_action` or `notify_user`, so the model doing nothing is visible as `no_action`.
- `NoteStore.record_outcome(note_id, outcome, result)`.
- The Notes page shows outcome and result in History. `list_scheduled` shows the action and the reminder for pending tasks.

## Error handling

| Situation | Behaviour |
|---|---|
| Action returns an error envelope (HA down, script missing, switch flipped off mid-run) | `outcome = failed`, `result` = the error message. A failure notification is sent and is never suppressed. |
| Notify fails | Logged. The task outcome still reflects the action. |
| LLM fallback run raises | `outcome = failed`, `result = "agent run failed"`. Logged as today. |
| Two tasks fire on the same event | Handled in order inside one runner call, under the existing runner lock. |

## Testing

- **Tool:** a valid action and params are stored; an unknown action, a non-ai action, the notify script and bad params are each rejected with guidance; a reminder is normalised to English; action and reminder together are accepted; neither is accepted (LLM fallback).
- **Store:** columns are added to an existing old-schema table; `record_outcome` works.
- **Runner:**
  - an action-only task calls the trigger tool and not the LLM, and records `done`;
  - a failure records `failed` and notifies even when suppressed;
  - a reminder is always sent;
  - an action plus reminder sends one combined notification;
  - a confirmation alone is skipped when suppressed;
  - a task with neither goes to the LLM fallback only for those tasks;
  - outcomes are recorded.
- **End to end:** the missing test from the review. A real `create_agent` graph with a scripted fake model, the real adapter tools and a fake REST client. Schedule via a tool call, push a websocket event into the listener, and assert `call_service("script", "ai_action_stop_vacuum")` happened with no model call at fire time.
- **Evals:** "once the vacuum starts, stop it" → `schedule_on_state_change(entity_id=vacuum.roborock_qrevo_s, to_state="cleaning", action_entity_id="script.ai_action_stop_vacuum")`.

## Non-goals

- More than one action per task (decision 3).
- Retrying a failed action.
- Recurring tasks.
