---
name: home_status_check
description: Playbook for a whole-house status roundup, e.g. leaving-home or bedtime "is everything safe?" checks
---
# Home status check

A roundup that aggregates several reads into one clear report. Run the checks
that fit the question (leaving home cares about locks/lights; bedtime cares about
the same plus who is home).

1. Call `get_person_locations` — who is home and who is away.
2. Call `list_entities` with `domain='lock'`, `state='unlocked'` — any lock left
   open. Repeat with `device_class='door'`, `state='open'` (and `window` if the
   user cares) for doors left open.
3. Call `list_entities` with `domain='light'`, `state='on'` — lights still on.
   Add `domain='switch'`, `state='on'` if the user asked about appliances.
4. Optional context, only if relevant to the question: `get_vacuum_state` (is the
   vacuum mid-clean?) and `get_weather` (windows open with rain coming?).

Report back: a short checklist grouped as who's home / what's unlocked or open /
what's still on, leading with anything a person would want to fix before leaving
or sleeping. If everything is secure, say so plainly.
