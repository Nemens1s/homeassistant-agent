---
name: what_happened_while_away
description: Playbook for summarizing house-wide activity over a past window, e.g. "what happened while I was out?"
---
# What happened while away

This is a house-wide activity question, NOT a single-entity trend. Use the
logbook, not `get_history`.

1. Call `get_person_locations` to establish the away window. Note when the person
   (or everyone) was `not_home`. If the user already gave an explicit window
   ("last hour", "since 3pm"), use that instead and skip this step.
2. Call `get_activity` for the matching range (`last_hour` / `last_24h` / `today` /
   `yesterday`). Omit `entity_id` — you want the whole house, not one device.
3. Group the results for the report: automations that fired, devices that changed
   state (lights, locks, doors, media), and anything that looks unexpected.
4. Only if the user asked about one specific thing (a door, a temperature),
   follow up with `get_history` on that single entity over the same window.

Report back: a short chronological summary of the notable events during the
window, leading with anything security- or safety-relevant (doors/locks opening,
motion while away).
