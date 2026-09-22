"""Build the fast-path backend + menu provider from settings, or None when off."""

from __future__ import annotations

import logging

from app.constants import AI_AUTOMATION_PREFIX_ACTION
from app.needle.menu import MenuProvider
from app.needle.remote_backend import RemoteNeedleBackend
from app.tools import registry

log = logging.getLogger("fast_path")


def build_fast_path_backend(cfg, rest, ctx):
    """Return (RemoteNeedleBackend, MenuProvider), or None when the fast path should not run.

    Disabled unless needle_enabled AND max_tier >= 2 (writes allowed). The
    returned backend + menu provider are passed to FastPathMiddleware via
    build_middleware so the existing ai_actions gate + audit apply unchanged."""
    if not cfg.needle_enabled or cfg.max_tier < 2:
        log.info("fast path off (needle_enabled=%s max_tier=%s)", cfg.needle_enabled, cfg.max_tier)
        return None
    if not cfg.needle_remote_url:
        log.warning("needle enabled but needle_remote_url is empty; fast path off")
        return None
    if registry.get("trigger_automation") is None:
        log.warning("needle enabled but trigger_automation not registered; fast path off")
        return None
    backend = RemoteNeedleBackend(cfg.needle_remote_url)
    menu_provider = MenuProvider(rest, ttl_s=cfg.needle_menu_ttl_s,
                                 prefix=AI_AUTOMATION_PREFIX_ACTION, ws=ctx.ws)
    return backend, menu_provider
