# Shared constants used across multiple modules.

# Any automation triggerable by the agent must have an entity_id starting with
# this prefix. Single source of truth — imported by trigger_action (enforcer)
# and list_actions (the agent's action menu).
AI_AUTOMATION_PREFIX = "automation.ai_"

# Narrower prefix used by the Needle fast-path menu: only real (non-test)
# action automations, not dev/test stubs.
AI_AUTOMATION_PREFIX_ACTION = "automation.ai_action"

# Any script triggerable by the agent must have an entity_id starting with
# this prefix. Single source of truth — imported by trigger_action (enforcer)
# and list_actions (the agent's action menu).
AI_SCRIPT_PREFIX = "script.ai_"

# Narrower prefix used by the Needle fast-path menu: only real (non-test)
# action scripts, not dev/test stubs.
AI_SCRIPT_PREFIX_ACTION = "script.ai_action"
