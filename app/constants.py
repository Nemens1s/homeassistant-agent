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

# An action is on the fast-path menu only when its HA description opts in with
# one of these markers; the text after the marker is what Needle matches on.
# Anything without a marker (e.g. the notify script) is agent-only.
FAST_PATH_MARKERS: tuple[str, ...] = ("NEEDLE:", "FAST-PATH:")

# An action may fire with no LLM call while the LLM is up (the strict action
# matcher) only when its HA description carries this flag, as a whole
# uppercase word. A flag, not a prefix: nothing after it is read. Leave it off
# anything whose name could also be a status question ("Garage Door Open").
EXACT_MATCH_MARKER = "EXACT-MATCH"

# Domains the REST client may write to when allow_actions is on. Writes are
# menu-only (automation.ai_* / script.ai_*, enforced elsewhere), so this is a
# fixed pair, not a user-configurable list.
WRITE_DOMAINS: tuple[str, ...] = ("automation", "script")