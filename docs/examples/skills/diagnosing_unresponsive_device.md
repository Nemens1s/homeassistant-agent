---
name: diagnosing_unresponsive_device
description: Playbook for a device that stopped responding, went unavailable, or shows a stale value
---
# Diagnosing an unresponsive device

1. Resolve the device to an entity. If the user gave a name (not an
   `entity_id`), call `search_entities` with that name and pick the matching
   entity. Then call `get_entity_state` on it.
   - If `state` is `unavailable` or `unknown`, the device is offline — continue.
   - If `state` looks normal but the value seems stale, note `last_updated` and
     compare it with how recently the user expected it to change.
2. Call `get_history` on the entity over `last_24h`. Find when it last reported a
   real value — that timestamp is when it dropped off.
3. Call `get_battery_status`. A dead or low battery is the most common cause for a
   sensor, remote, or lock going quiet. If the device is on that list and low,
   that is very likely the answer.
4. Call `get_activity` (range `last_24h`, `entity_id` set to the device) to see if
   it flapped between available and unavailable, or changed just before going
   quiet.
5. Call `get_error_log` and look for the device or integration name — connectivity
   drops, auth failures, and integration errors show up here.

Report back: whether the device is actually offline, when it went quiet, and the
single most likely cause (dead battery, connectivity/integration error, or truly
just idle).
