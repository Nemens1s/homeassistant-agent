"""Local HA Agent: exposes the App's agent as an Assist conversation agent."""

# NOTE: the `homeassistant` imports are deferred into the function bodies (and
# type hints are stringified via `from __future__ import annotations`) so that
# importing sibling modules (e.g. `client`, which is HA-free) does not require
# `homeassistant` to be installed. Inside HA core the package is always present,
# so this changes nothing at runtime; it only keeps `client.py` unit-testable
# without the HA harness.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS = ["conversation"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
