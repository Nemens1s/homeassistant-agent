"""Build the FastPathRouter from settings, or None when disabled."""

from __future__ import annotations

import logging

from app.constants import AI_AUTOMATION_PREFIX_ACTION
from app.needle.menu import MenuProvider
from app.needle.remote_backend import RemoteNeedleBackend
from app.needle.router import FastPathRouter
from app.tools import registry
from app.tools.adapter import LoopGuard, to_structured_tool

log = logging.getLogger("needle")

def build_fast_path_router(cfg, rest, ctx):
    """Return a FastPathRouter, or None when the fast path should not run.

    Disabled unless needle_enabled AND max_tier >= 2 (writes allowed). The router
    invokes the EXISTING trigger_automation StructuredTool, so the ai_actions
    gate + audit apply unchanged."""
    if not cfg.needle_enabled or cfg.max_tier < 2:
        log.warning(f"needle status {cfg.needle_enabled}; max-tier {cfg.max_tier}")
        return None
    if not cfg.needle_remote_url:
        log.warning("needle enabled but needle_remote_url is empty; fast path off")
        return None
    trigger_defn = registry.get("trigger_automation")
    if trigger_defn is None:
        log.warning("needle enabled but trigger_automation not registered; fast path off")
        return None
    trigger_tool = to_structured_tool(trigger_defn, ctx, LoopGuard())
    backend = RemoteNeedleBackend(cfg.needle_remote_url)
    menu_provider = MenuProvider(rest, ttl_s=cfg.needle_menu_ttl_s, prefix=AI_AUTOMATION_PREFIX_ACTION, ws=ctx.ws)
    return FastPathRouter(backend, menu_provider, trigger_tool,
                          cfg.needle_confidence_threshold)
