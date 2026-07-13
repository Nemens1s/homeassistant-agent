---
name: diagnosing_automations
description: Playbook for finding out why an automation did not fire or misbehaved
---
# Diagnosing an automation

1. Call `get_automations` with the automation's entity_id.
   - If `state` is `off`, the automation is disabled — that is the answer.
   - Note `last_triggered` and compare it with when the user expected it to fire.
2. Fetch the config (same call) and identify the trigger entities and conditions.
3. Call `get_history` on each trigger entity over the window when the automation
   should have fired. Did the trigger condition actually occur?
4. Call `get_logbook` around the expected time — did the automation appear
   (fired but wrong action) or is it absent (never triggered)?
5. Call `get_error_log` and look for the automation's name or template errors.

Report back: whether the automation is enabled, whether its trigger actually
happened, whether it fired, and the single most likely cause.
