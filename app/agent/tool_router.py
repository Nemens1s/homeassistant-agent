"""Per-query tool subsetting.

17 tool schemas on every call is a heavy, permanent context tax on a small model
and a real driver of wrong selections. This narrows the menu to the latest user
message: a small CORE of general-purpose tools is always offered, and specialized
"intent shortcut" tools are added only when their keywords appear.

Design constraint: subsetting decides *availability*, never *choice*. It must never
hide a tool the model legitimately needs, so the general query tools
(search/list/state) stay in CORE as a fallback — a missed keyword degrades to a
slightly slower path, never a dead end. When in doubt it exposes more, not less.
"""

from __future__ import annotations

# Always offered: general-purpose querying + control. These cover any request even
# when no specialized keyword matches, so hiding a shortcut can only cost a step,
# never capability.
CORE_TOOLS: frozenset[str] = frozenset({
    "search_entities",
    "get_entity_state",
    "list_entities",
    "load_skill",
    "control_entity",      # tier-gated at build time; harmless if absent
    "trigger_automation",
})

# Room/area overview intents. Both device-level room tools are offered together so
# the model can answer "what's in <room>" with names-only topology (get_areas) or the
# flat device list (list_devices) — never with noisy entity lists. Keyed on intent
# PHRASES, not the bare token "room" (which fires on entity_ids like
# sensor.living_room_temperature and distracts the model).
_ROOM_OVERVIEW: tuple[str, ...] = (
    "rooms", "areas", "which room", "what room", "list rooms", "in the",
    "what devices", "devices in", "home map", "topology", "layout", "overview",
    "all rooms", "every room", "what do i have", "what's in", "whats in", "in my",
)

# tool -> trigger substrings (matched against the lowercased message). Generous by
# design: false positives just add a schema; false negatives hide a shortcut.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "get_battery_status": ("batter", "charge", "charging", "power level", "low power"),
    "get_vacuum_state": ("vacuum", "roomba", "roborock", "hoover", "robot", "cleaning", "clean the"),
    "get_weather": ("weather", "forecast", "rain", "sunny", "snow", "wind", "outside", "humidity outside"),
    "get_person_locations": (
        # bare "home" catches "Is <name> home?"; over-exposing the person tool is
        # harmless (it's a candidate, not a forced choice).
        "home", "person", "people", "location", "where is", "presence", "away",
        "everyone", "nobody", "anyone",
    ),
    "get_history": (
        "history", "trend", "over time", "time series", "timeseries", "graph", "chart",
        "past ", "yesterday", "last week", "last hour", "last 24", "last 7", "last 30",
        "last month", "over the",
    ),
    "get_logbook": (
        "logbook", "happened", "what happened", "event", "activity", "occurred", "occur",
        "recently", "log of", "did ", " ran", "run today",
    ),
    "get_error_log": (
        "error", "healthy", "health", "crash", "warning", "broken", "not working", "problem",
    ),
    "get_automations": (
        "automat", "routine", "trigger", "enabled", "disabled", "runs when", "run when",
    ),
    # Both device-level room tools share the overview triggers (see _ROOM_OVERVIEW).
    "get_areas": _ROOM_OVERVIEW,
    "list_devices": _ROOM_OVERVIEW,
}


def select_tool_names(available: set[str] | frozenset[str], message: str) -> set[str]:
    """Return the subset of `available` tool names to expose for `message`.

    CORE tools (that are available) are always included; specialized tools are
    included when a keyword matches. Never returns empty as long as `available`
    is non-empty.
    """
    text = message.lower()
    selected = set()
    for name in available:
        if name in CORE_TOOLS:
            selected.add(name)
    for name in available:
        for kw in _KEYWORDS.get(name, ()):
            if kw in text:
                selected.add(name)
                break
    # Safety net: if somehow nothing matched (e.g. a tool set with no CORE members),
    # fall back to offering everything rather than starving the model.
    return selected or set(available)


def select_tools(tools: list, message: str) -> list:
    """Filter a list of tool objects (anything with a `.name`) by `select_tool_names`,
    preserving input order."""
    available = set()
    for t in tools:
        available.add(t.name)
    keep = select_tool_names(available, message)
    result = []
    for t in tools:
        if t.name in keep:
            result.append(t)
    return result
