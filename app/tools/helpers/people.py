"""Person name aliasing shared across read tools.

Aliases live in Settings.person_name_map today (list of {ha_name, name}); an
HA-sourced map would slot in behind these two functions without touching
callers. Multiple ha_names pointing at the same canonical name are treated as
aliases of each other — the agent uses them to connect a query like "sonja's
battery" to the canonical person "Sofija".
"""

from __future__ import annotations


def alias_legend(settings) -> dict[str, list[str]]:
    """Map each canonical name to the sorted list of other names that alias it.

    Canonical names with only a single associated name (no aliases) are omitted,
    so the result is empty unless real aliases are configured.
    """
    grouped: dict[str, list[str]] = {}
    for ha_name, canonical in settings.person_name_lookup.items():
        grouped.setdefault(canonical, []).append(ha_name)

    legend: dict[str, list[str]] = {}
    for canonical, names in grouped.items():
        if len(names) > 1:
            legend[canonical] = sorted(names)
    return legend


def resolve_name(friendly_name: str, settings) -> tuple[str, list[str]]:
    """Resolve an HA friendly_name to (canonical, aliases).

    aliases are the other ha_names pointing at the same canonical name, minus
    the one reported. Unknown names pass through unchanged with no aliases.
    """
    lookup = settings.person_name_lookup
    canonical = lookup.get(friendly_name, friendly_name)

    aliases: list[str] = []
    for ha_name, c in lookup.items():
        if c == canonical and ha_name != friendly_name:
            aliases.append(ha_name)
    aliases.sort()
    return canonical, aliases
