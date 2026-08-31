# Shared constants used across multiple modules.

# Any automation triggerable by the agent must have an entity_id starting with
# this prefix. Single source of truth — imported by trigger_automation (enforcer)
# and get_automations (ai_controllable flag).
AI_AUTOMATION_PREFIX = "automation.ai_"

# Narrower prefix used by the Needle fast-path menu: only real (non-test)
# action automations, not dev/test stubs.
AI_AUTOMATION_PREFIX_ACTION = "automation.ai_action"
