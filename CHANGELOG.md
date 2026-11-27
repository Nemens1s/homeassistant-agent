# Changelog

Notable changes to Agent Gosling. The add-on store shows this file.
Every merge to `main` is a release. A PR names the version its merge
produces: the next patch by default (the bump workflow sets it), or a
minor/major version set by hand in `pyproject.toml` + `config.yaml`
(the workflow then leaves it alone).

## 0.5.1

### Added

- `audit_retention_days` (default 365, 0 keeps all): action audit rows older
  than this are deleted daily with the other retention prunes.

### Changed

- `languages` now defaults to `[en]` everywhere (it was `[en, ru, et]`
  outside the add-on). Set the household's list in the add-on options.
- `get_entity_state` returns at most 12 attributes beyond the basics and
  replaces long lists and nested dicts (e.g. a weather forecast) with
  `"<n items>"`, flagging `attributes_truncated`.
- The history budget is computed from the measured size of the system prompt
  and tool schemas instead of a fixed 2048-token guess. A context window too
  small for them is logged as a warning at startup.

### Fixed

- A turn stopped at the step limit no longer leaves an unanswered tool call
  in the conversation, which broke the next turn on some chat templates.
- Script configs are cached for 60 s, cutting the per-turn REST calls of
  `list_actions`, action checks and the fast-path menu.
- The language detector's per-conversation memory is bounded (500 threads).

## 0.5.0

### Added

- Pre-fetch for single-intent reads (`prefetch_enabled`, on by default): a
  question that names one entity_id, asks about a room, or matches one
  zero-argument read tool (weather, batteries, presence, vacuum, errors,
  scheduled notes) runs that tool before the first model call, so the model
  only phrases the answer. Shows as `gosling.path = prefetch` in telemetry.

## 0.4.44

- Templated reply after a single successful agent `trigger_action` (no
  second model call).
