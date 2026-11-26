# Changelog

Notable changes to Agent Gosling. The add-on store shows this file.
Every merge to `main` is a release. A PR names the version its merge
produces: the next patch by default (the bump workflow sets it), or a
minor/major version set by hand in `pyproject.toml` + `config.yaml`
(the workflow then leaves it alone).

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
