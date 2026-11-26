# Changelog

Notable changes to Agent Gosling. The add-on store shows this file.
New entries go under **Unreleased**; the version is bumped after merge.

## Unreleased

### Added

- Pre-fetch for single-intent reads (`prefetch_enabled`, on by default): a
  question that names one entity_id, asks about a room, or matches one
  zero-argument read tool (weather, batteries, presence, vacuum, errors,
  scheduled notes) runs that tool before the first model call, so the model
  only phrases the answer. Shows as `gosling.path = prefetch` in telemetry.

## 0.4.44

- Templated reply after a single successful agent `trigger_action` (no
  second model call).
