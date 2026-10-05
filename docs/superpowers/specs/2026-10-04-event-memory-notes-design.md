---
repo: homeassistant-agent
date: 2026-10-04
status: draft (design approved in chat, spec awaiting review)
---

# Event-triggered memory notes: design

## Context

The user skips the "vacuum when we leave" automation by turning it off in HA. They then forget to turn it back on, so the next legitimate run doesn't happen. They want to tell the agent instead:

- "We're leaving. Once the vacuum starts, stop it."
- "When we get home, remind me to do X."
- "At 18:00 remind me to call mum." / "In 2 hours turn off the lights."

The agent saves a **one-shot note** tied either to an HA state change or to a wall-clock time. When the trigger happens, the harness finds matching notes **without the LLM**. It then runs the agent headlessly with only those notes, and the agent acts through the existing menu-only action path.

The original spec listed this as a non-goal (`2026-07-13-local-ha-agent-design.md:34`). The telemetry spec anticipates it (`2026-09-17-agent-telemetry-design.md:47-49`). The automation-mediated control spec deferred WebSocket subscriptions "until a proactive use case justifies the machinery" (`2026-08-24-automation-mediated-control-design.md:37-40`). This is that use case.

### Success criteria

- When the user says "once the vacuum starts, stop it", a pending note is stored, and nothing happens to the vacuum right away.
- When the vacuum next switches to `cleaning`, `script.ai_action_stop_vacuum` is triggered within one agent run, and the note is marked fired.
- The run after that proceeds normally, because nothing stays switched off.
- "At 18:00 remind me to X" sends a notification in the user's language at about 18:00 (within one minute).
- An event or clock tick with no matching note costs **zero** LLM calls.
- The list of watched entities is set in the add-on config, with no code change.

## Decisions

| Topic | Decision |
|---|---|
| Action path | Menu-only stays. Event runs act only through `trigger_action` on `script.ai_*` / `automation.ai_*`, under the ai-actions switch gate. Notifications go through a dedicated `notify_user` tool, which itself calls a menu script. There is no new write surface. |
| Triggers | Two kinds. **State**: `entity_id` (must be watched) plus an optional `to_state`. **Time**: a local wall-clock `fire_at`, driven by the HA clock entity `sensor.europe_tallinn`. Both are set when the note is written, and matching is an SQL query. Notes that don't match never reach the LLM. |
| Lifecycle | One-shot. State notes expire after 24h by default, and the agent may set a longer expiry. Time notes expire a grace period after `fire_at`. Fired, expired and cancelled notes stay in the DB for history and are never matched again. |
| Management | Chat (`list_memory_notes`, `cancel_memory_note`) plus a web UI page (list and delete). |
| Feedback | Event runs go to a dedicated `events` thread that is visible in the UI. The agent **always** calls `notify_user` after acting. Python decides whether the notification is actually delivered (see §7). The agent never knows about the toggle. |
| Language | The agent core is English-only. Python translates note text **to English** when it is saved, and notification text **from English** to the user's language when it is sent. Both use the existing lang-mt client and fail open. |
| HA scripts | This repo only defines the **contract** for HA scripts. The scripts themselves are written in `SmartHome/Suur-Ameerika/ai_actions/`. |

## Non-goals

- Recurring rules ("every time we leave…", "every day at 8").
- Replaying state events missed while the WebSocket was down. Time notes do catch up; see §4.
- Free-text or vague notes with no trigger attached.
- Writing to HA outside the `ai_*` menu.

## HA contract

Scripts live in `SmartHome/Suur-Ameerika/ai_actions/` and follow the house conventions:

- `alias: "AI Action: …"`
- `description` ending in a `NEEDLE:` line
- `fields` with selectors
- the first step gates on `input_boolean.ai_triggered_actions` being `on`
- `mode: single`

### Existing, used as-is

- `script.ai_action_stop_vacuum`: stops the Roborock and sends it to the dock.
  - It currently has **no** gate step. The adapter-side gate still applies, but adding the step keeps it consistent with the other scripts.
- `sensor.europe_tallinn`: the World Clock integration. Its state is local time in the format `HH:MM DD-MM-YYYY` (for example `09:32 05-10-2026`) and it updates every minute.

### New: `script.ai_action_notify`

| Field     | Type | Required | Notes |
| --------- | ---- | -------- | ----- |
| `message` | text | yes      | Plain text, at most about 500 chars, already in the user's language. The agent writes English and Python translates before calling the script (see §7). The script never translates. |
| `title`   | text | no       | Defaults to "Gosling". |

- **Behaviour:** sends `message` to the household's phone(s) through whichever notify service(s) the user picks. Delivery targets are the script's concern, not the agent's.
- **Gate:** the usual `input_boolean.ai_triggered_actions` condition.
- **NEEDLE line:** required by convention. The script is excluded from the fast-path menu and from `list_actions` anyway (§7), so the wording doesn't matter much. For example: `NEEDLE: Send a push notification message to the household phones.`

`settings.notify_action` (default `script.ai_action_notify`) names this script. Only `notify_user` calls it.

## Architecture

### 1. WebSocket subscriptions (`app/ha/websocket.py`)

**API**
- `subscribe(message: dict, callback) -> int`
  - Sends the message with a fresh id and stores `_subscriptions[id] = (message, callback)`.
  - Allowlist: a new tuple `SUBSCRIPTION_COMMANDS = ("subscribe_trigger",)`, enforced like `READ_ONLY_COMMANDS`.

**Event routing**
- `_dispatch` checks `type == "event"` and looks the id up in `_subscriptions` **before** `_pending`.
- The callback is scheduled with `asyncio.create_task`, so a slow handler never blocks the reader.
- Callback exceptions are logged and never propagate.

**Reconnect**
- After each `auth_ok` in `_run`, every stored subscription is re-sent with a new id, and the dict is re-keyed.
- `_handle_disconnect` keeps the subscriptions; it clears only the pending futures and the cache, as today.

**Startup fix**
- Today `main.py:187-191` sets `ws = None` for the life of the process if the first connect fails.
- Change: always build and start the client, and let `_run`'s existing backoff handle the initial connect.
- Tools still see a "not connected" client as degraded, through the existing `ws_unavailable` path. They check a `connected` property instead of `ws is None`.
- The listener depends on this fix. Without it, a single slow HA boot disables events until the add-on restarts.

### 2. Config (`app/config.py`, `config.yaml`)

New `Settings` fields, each mirrored in `config.yaml` `options:` and `schema:`:

| Field | Type / default | Schema |
|---|---|---|
| `watched_entities` | `list[str] = []` | `["str"]` |
| `clock_entity` | `str = "sensor.europe_tallinn"` | `str?` (empty means time notes are off) |
| `event_confirmations_enabled` | `bool = True` | `bool` |
| `memory_note_default_ttl_hours` | `int = 24` | `int(1,720)` |
| `memory_note_max_ttl_hours` | `int = 168` | `int(1,720)` |
| `time_note_grace_minutes` | `int = 120` | `int(1,1440)` |
| `notify_action` | `str = "script.ai_action_notify"` | `str` |

**Feature switches**
- If `watched_entities` is empty, state notes are off.
- If `clock_entity` is empty, time notes are off.
- In either case the corresponding save tool returns `feature_disabled` with a clear message.

### 3. Clock parsing (`app/events/clock.py`)

- `CLOCK_FORMAT = "%H:%M %d-%m-%Y"`.
- `parse_clock(state: str) -> datetime | None` returns a **naive local** datetime, or `None` for `unavailable` / `unknown` / garbage.
- `to_key(dt) -> "YYYY-MM-DD HH:MM"` is the sortable string stored in the DB, so SQL can compare it lexically.

**Timezones.** All time-note maths stays in the clock's local wall time. Python never converts timezones, so the container's TZ doesn't matter. During the DST fall-back hour a wall time occurs twice, and the note fires on the first one. That is accepted.

**Current time.** `current_local_time(ctx) -> datetime` does a point-read of `clock_entity` via `ctx.rest.get_state` and returns `parse_clock`'s result. It returns `None` if the clock is unreadable.

### 4. Note store (`app/memory/store.py`)

`NoteStore` adds a table to the **checkpoint DB file** (one file to back up). It follows the `LangOverlay` / `AuditSink` pattern:
- sync `sqlite3`, WAL, `CREATE TABLE IF NOT EXISTS`
- a `threading.Lock` around every access; calls are synchronous like `LangOverlay` (each is one indexed query)
- **never raises**: it logs and degrades

**Fallbacks**
- If `checkpoint_db_path` is empty, it uses an in-memory SQLite DB, like the checkpointer's memory fallback.

```sql
CREATE TABLE IF NOT EXISTS memory_notes (
  id                   INTEGER PRIMARY KEY,
  trigger_kind         TEXT NOT NULL,             -- state|time
  kind                 TEXT NOT NULL,             -- action|reminder
  created_at           TEXT NOT NULL,             -- ISO-8601 UTC
  -- state notes
  entity_id            TEXT,
  to_state             TEXT,                      -- NULL = any state change
  expires_at           TEXT,                      -- ISO-8601 UTC (state notes)
  -- time notes (clock-local "YYYY-MM-DD HH:MM", see §3)
  fire_at_local        TEXT,
  expires_at_local     TEXT,                      -- fire_at + grace
  -- content
  instruction          TEXT NOT NULL,             -- always English
  instruction_original TEXT,                      -- as given, when it differed
  language             TEXT NOT NULL DEFAULT 'en',-- user's language for notifications
  tags                 TEXT NOT NULL DEFAULT '[]',
  status               TEXT NOT NULL DEFAULT 'pending', -- pending|fired|expired|cancelled
  fired_at             TEXT,
  source_thread_id     TEXT
);
CREATE INDEX IF NOT EXISTS memory_notes_state ON memory_notes(status, entity_id);
CREATE INDEX IF NOT EXISTS memory_notes_time  ON memory_notes(status, fire_at_local);
```

**Methods**
- `add(...) -> id`
- `list_pending()`
- `cancel(id) -> bool`
- `match_state(entity_id, new_state, now_utc) -> list[Note]`
  - First marks pending state notes past `expires_at` as `expired`.
  - Then returns pending notes where `entity_id = ?` and (`to_state IS NULL` or `to_state = ?`), oldest first.
- `due_time(now_local_key) -> list[Note]`
  - First marks pending time notes whose `expires_at_local <= now` as `expired`.
  - Then returns pending notes where `fire_at_local <= now`, ordered by `fire_at_local`.
  - Using `<=` instead of `=` means a missed tick, for example during a reconnect, fires on the next tick.
- `mark_fired(ids, now_utc)`

There is no sweeper task.

### 5. Event listener (`app/events/listener.py`)

`EventListener(ws, notes, run_event, settings)` is started in the `main.py` lifespan **inside** the `open_checkpointer` block, after `app.state.agent` exists, and cancelled in `finally`.

**Subscription.** On start, it sends one subscription covering `watched_entities` plus `clock_entity`, if either is set:

```json
{"type": "subscribe_trigger",
 "trigger": {"platform": "state", "entity_id": [...watched, clock], "to": null}}
```

`to: null` means real state changes only; attribute-only updates are ignored.

**Each event**
1. Read `entity_id`, `from_state.state` and `to_state.state` from `variables.trigger`.
2. Ignore transitions to or from `unavailable` / `unknown`.
3. If the entity is the clock: parse it, then run `notes.due_time(key)`. Otherwise run `notes.match_state(...)`.
   - If nothing matches, log `event entity=... matched=0` at DEBUG and **stop**, with no LLM call. Clock ticks with no due notes are not logged at all; once a minute would be noise.
4. Run `notes.mark_fired(ids)` **before** the run, so a crash can't make a note fire twice.
5. `await run_event(trigger_description, notes)`. Runs are serialized by one `asyncio.Lock`, so two event runs never act at the same time.

### 6. Headless event run (`app/events/runner.py`)

`run_event` reuses `app.state.agent`, inside a `RunScope` (§8):

```python
with run_scope(language=batch_language, suppress_notify=suppress):
    await agent.ainvoke(
        {"messages": [HumanMessage(event_message)]},
        config={"configurable": {"thread_id": "events"},
                "recursion_limit": settings.recursion_limit},
        context={"fast_path": False},
    )
```

- **Fast path off:** `context=` is what `FastPathMiddleware._opted_out` reads (`fast_path_middleware.py:45-52`). The telemetry spec's `configurable.fast_path` form is wrong.
- **Language layer:** the HTTP-edge `inbound()` / `outbound()` does not run. The event message and the agent's reasoning are English. Only notification text is translated, inside `notify_user`.

**Run settings** (computed in Python, invisible to the agent)
- `suppress = not settings.event_confirmations_enabled and every note in the batch has kind == "action"`. A batch containing any reminder always delivers its notifications.
- `batch_language` = the `language` of the earliest note in the batch. In practice every note in a household batch has the same language.

**Event message** (a pure function, `build_event_message(trigger, notes)`). It is identical whatever the toggle says:

```
[EVENT] vacuum.roborock_qrevo_s changed docked → cleaning at 10:02.
Saved notes for this event:
- #12 (action, saved 09:14): "Stop cleaning and send the vacuum to the dock."
Carry out each note now using your tools (list_actions → trigger_action).
For a reminder note, deliver the reminder text with notify_user.
After acting, call notify_user once with a short summary of what you did.
Do not ask questions; nobody is reading this thread live.
```

For a time trigger, the first line reads `[EVENT] Scheduled time 18:00 05-10-2026 reached.`

**Prompt guidance.** A short "MEMORY NOTES" paragraph in `build_system_prompt` (`app/agent/factory.py`) covers:
- *Writing:*
  - Use `save_event_note` for "when/once X happens" requests and `save_time_note` for "at HH:MM / in N minutes" requests.
  - Set `kind="reminder"` when the user wants to be told something, and `kind="action"` when the house should do something.
  - Confirm to the user what was saved.
- *Event runs:* messages starting with `[EVENT]` come from the harness and must be followed as written.

**Telemetry.** The run goes through the normal telemetry middleware. `thread_id="events"` is enough to tell event runs apart for now. `notify_user` records whether it delivered or suppressed (§7), so the decision trail is complete.

### 7. `notify_user` tool (`app/tools/action/notify_user.py`)

`notify_user(message: str, title?: str)` uses `Tier.ACTION`, so the ai-actions gate, the audit row and the loop guard all apply through the adapter, as for `trigger_action`.

**Handler**
1. Validate `settings.notify_action` (`script.ai_*` prefix, `script` in `allowed_domains`). If it fails, return `not_configured`.
2. Read the current `RunScope`. If `suppress_notify` is set, log `agent.actions notify suppressed=1 message=...` and return **the same success envelope** a delivery returns: `{"sent": true}`. The agent cannot tell the difference, by design.
3. Translate `message` (and `title`) from English to `scope.language` with `lang.from_english(...)` (§9). This fails open: if translation fails, the English text is sent.
4. Call `rest.call_service("script", <object_id>, data={"message": ..., "title": ...})` and return `{"sent": true}`.

**Single notify path**
- The script named by `notify_action` is filtered out of `list_actions` and the fast-path menu.
- `trigger_action` rejects it with `use_notify_user`.
- This keeps suppression and translation impossible to bypass.

**Availability.** `notify_user` is offered in event runs: the tool router adds it when the message starts with `[EVENT]`. It is also offered in chat on notify keywords ("notify", "notification", "message me", "send me").

### 8. Run scope (`app/agent/run_scope.py`)

`RunScope(language="en", suppress_notify=False, thread_id="default")`. Callers
put `gosling_language` / `gosling_suppress_notify` into the LangGraph
`configurable` next to `thread_id` (`scope_configurable(...)`): `run_event`
(§6) and the chat endpoints (language = `inbound.language`, never
suppressed). The tool adapter builds the scope from the config every tool call
receives and sets it (a ContextVar) only around the handler call, so handlers
read `current_scope()` and nothing depends on context propagating through
LangGraph's task scheduling.

### 9. Language normalisation (`app/i18n/adapter.py`)

Two public, fail-open methods are added to `LanguageAdapter` and `NoopLanguageAdapter`. Both reuse the existing glossary masking (`[E1]` placeholders):

| Method | Behaviour | On error | Noop version |
|---|---|---|---|
| `to_english(text) -> (english, src_lang)` | `translate(src="auto", tgt="en", allowed=languages)` | returns `(text, "en")` and logs `agent.i18n` | returns `(text, "en")` |
| `from_english(text, language) -> str` | `translate(src="en", tgt=language)` when `language != "en"` | returns `text` | returns `text` |

`ToolContext` gains `lang` (the same adapter instance built in the lifespan).

**When a note is saved**
- `instruction` is passed through `to_english`, and the English text is what gets stored.
- If translation changed anything, the original is stored in `instruction_original`, so the UI can show what the user actually said.
- `language` is taken from `RunScope.language`, not from detecting the instruction text. With the language layer on, the agent already sees English text, so detection would always say "en".
- This catches non-English text that reaches the agent without going through the language layer, for example the agent copying a Russian friendly name, or the layer being bypassed. Small models and Needle only ever see English instructions.

### 10. Memory tools (`app/tools/memory/`)

Each tool is its own module, added to `_DEFAULT_MODULES`. `ToolContext` gains `notes: NoteStore | None`; tools return `ToolResult.error("notes_unavailable", ...)` when it is `None`.

There are two save tools, not one tool with a trigger-type union, because small models handle flat schemas far better.

| Tool | Params | Behaviour |
|---|---|---|
| `save_event_note` | `entity_id`, `to_state?`, `instruction`, `kind: action\|reminder`, `tags?`, `expires_in_hours?` | Rejects with `entity_not_watched` (data: the watched list) unless the entity is watched. Clamps the TTL to `memory_note_max_ttl_hours`. Returns `{id, entity_id, to_state, expires_at}`. |
| `save_time_note` | `at?: "HH:MM" or "HH:MM DD-MM-YYYY"`, `in_minutes?: int`, `instruction`, `kind`, `tags?` | Exactly one of `at` / `in_minutes` is required (`invalid_params` otherwise). "Now" comes from `current_local_time(ctx)`; if it is `None`, returns `clock_unavailable`. A bare `HH:MM` that has already passed today means tomorrow. A past full date is rejected with `in_past`. Stores `fire_at_local` and `expires_at_local = fire_at + grace`. Returns `{id, fire_at: "HH:MM DD-MM-YYYY"}` in the clock's format, so the agent can echo it back. |
| `list_memory_notes` | none | Pending notes: `id`, trigger (entity/state or fire time), kind, instruction (English), created, expires. |
| `cancel_memory_note` | `id` | Sets the status to `cancelled`. Returns `not_found` for unknown or non-pending ids. |

- **Tier: `Tier.READ`.** These tools change agent-local state only, never HA. The ai-actions gate protects **HA writes**, and saving a note while the switch is off is legitimate: the switch may be back on by the time the note fires. Documented in the tool docstrings and in CLAUDE.md.
- **Tool routing** (`app/agent/tool_router.py`): keyword triggers ("when", "once", "after", "at", "in", "remind", "remember", "note", "notes", "reminder", "cancel", "tomorrow", "minutes", "hours") offer the four memory tools. "at" and "in" are whole-word matches and only count alongside a digit, to avoid noise. The tools are not CORE.

### 11. Fast-path guard

Without a guard, "once the vacuum starts, stop it" could be classified by Needle as `script.ai_action_stop_vacuum` and **run right away**, which is the opposite of what the user meant.

- **Requirement:** before classifying, `FastPathMiddleware` skips (falls through to the LLM) when the message contains a deferral cue.
- `has_deferral_cue(text)` is a pure function: a whole-word regex over `when|once|after|if|remind|later|tomorrow|tonight|next time|as soon as|in \d+|at \d{1,2}[:.]\d{2}`.
- This errs toward the LLM, which costs latency but never correctness.
- With the language layer on, the guard sees the English translation, so one list is enough.

### 12. HTTP + UI

- `GET /api/notes?status=pending|all` returns a list of notes.
- `DELETE /api/notes/{id}` cancels a note, using the same `cancel` semantics.
- `frontend/notes.html` + `notes.js` + `notes.css` follow the `actions.html` page:
  - A table of pending notes with columns: trigger (entity → state, or fire time), kind, instruction (the original text when present, English on hover), saved, expires. Each row has a delete button.
  - A collapsed "history" section showing recent fired, expired and cancelled notes.
- Linked from the main UI next to Actions.
- The `events` thread is opened through the existing history view (`/api/history?thread_id=events`).

## Error handling

| Situation | Behaviour |
|---|---|
| WS disconnected | Listener idles. Subscription re-sent on reconnect. Missed state events are not replayed. Time notes catch up on the next tick, within their grace period. |
| Clock entity unavailable | No ticks, so time notes wait. Once the clock returns, they fire if still inside the grace period, otherwise they expire. `save_time_note` returns `clock_unavailable`. |
| Feature switched off (empty `watched_entities` / `clock_entity`) | That trigger type isn't subscribed, and its save tool returns `feature_disabled`. |
| AI switch off when a note fires | `trigger_action` and `notify_user` both return `ai_disabled`, so nothing is delivered. The note stays `fired`, and the attempt is visible in the `events` thread and telemetry. Accepted: the user turned the AI off on purpose. |
| lang-mt down | Notes are stored as given (`language` still recorded). Notifications are sent in English. |
| Agent run raises | Logged with the note ids. Listener keeps running. Notes stay `fired`. |
| Note store errors | Logged. Matching returns `[]`, so the event is ignored, and tools return `notes_unavailable`. |
| Burst of events | Serialized by the lock. One-shot status prevents double-firing on flapping states. |

## Testing (pytest, no HA/Ollama)

- **WebSocket:** a fake connection delivers `type:event` messages to the subscription callback. After a simulated reconnect, the subscription is re-sent with a new id and events route again. Non-allowlisted subscribe types are rejected. Startup with HA down: the client is created, `connected` is false, and it connects later.
- **Clock:** `parse_clock("09:32 05-10-2026")` gives the right datetime; garbage and `unavailable` give `None`; `to_key` ordering matches chronological order.
- **NoteStore:**
  - State: match on entity and state; `to_state` NULL matches any state; expiry.
  - Time: due when `fire_at <= now`; catch-up after a skipped tick; expiry after the grace period.
  - General: cancel; fired notes are not re-matched.
- **Save tools:**
  - An unwatched entity is rejected with the watched list; the TTL is clamped.
  - Time: `at` with a passed `HH:MM` rolls to tomorrow; `in_minutes`; a past full date is rejected; clock unreadable gives `clock_unavailable`.
  - The instruction is normalised to English and the original kept (fake adapter); `language` comes from the `RunScope`.
- **`notify_user`:**
  - Suppressed scope: no service call, but the envelope is identical to a real send.
  - Normal scope: translated text reaches `call_service`; the English fallback is used on adapter failure.
  - `trigger_action` on `notify_action` gives `use_notify_user`; `notify_action` is absent from `list_actions`.
- **Run scope:** a tool invoked inside `agent.ainvoke` (fake LLM) sees the scope it was called under.
- **Listener / runner:**
  - An unmatched event or clock tick means the fake `run_event` is not called.
  - `unavailable` transitions are ignored.
  - Notes are marked fired before the run; concurrent events are serialized.
  - `suppress` is true only when the toggle is off and every note is an action.
  - The event message is byte-identical for both toggle values.
- **Fast-path guard:** `has_deferral_cue` cases, and the middleware falls through on a cue.
- **API:** notes list and delete.
- **Evals** (`tests/evals`):
  - "once the vacuum starts, stop it" calls `save_event_note(entity_id="vacuum.roborock_qrevo_s", to_state="cleaning", kind="action")`, not `trigger_action`.
  - "when we get home remind me to take out the trash" calls `save_event_note` on the presence entity with `kind="reminder"`.
  - "remind me at 18:00 to call mum" calls `save_time_note(at="18:00", kind="reminder")`.

## Verification (live)

1. In the SmartHome repo, create `script.ai_action_notify` following the contract, and add the gate step to `ai_action_stop_vacuum`.
2. Set `watched_entities: [vacuum.roborock_qrevo_s, <presence input_boolean>]` and leave `clock_entity` at its default.
3. Say "once the vacuum starts, stop it". The vacuum must not stop, and the note must appear on `/notes.html`.
4. Start the vacuum in HA. It should stop and dock, a confirmation notification should arrive, the note should show `fired`, and the `events` thread should show the run.
5. Start the vacuum again. It should keep cleaning.
6. Turn off `event_confirmations_enabled` and repeat. The agent still calls `notify_user` (visible in the thread), the log shows `suppressed=1`, and no notification arrives.
7. With the language layer on, say in Russian "remind me in 2 minutes to check the oven". The note shows the Russian original and stores English. About 2 minutes later a Russian notification arrives, even with confirmations off, because it is a reminder.
8. Toggle presence with no notes. The logs should show `matched=0` and no LLM call.
