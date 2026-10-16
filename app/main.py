"""Add-on entrypoint: FastAPI app factory. Run with
`uvicorn app.main:create_app --factory` — the factory keeps construction
config-injected and testable (no import-time singletons)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent.checkpointer import open_checkpointer
from app.agent.factory import build_agent, build_system_prompt
from app.agent.llm_health import (
    build_fast_path_probe,
    build_llm_probe,
    needs_reresolve,
    reresolve,
)
from app.agent.llm_probe import resolve_llm_settings
from app.api import actions as actions_api
from app.api import chat as chat_api
from app.api import notes as notes_api
from app.api import system as system_api
from app.api import telemetry as telemetry_api
from app.audit import AuditSink
from app.config import Settings, load_settings, warn_if_llm_unset
from app.constants import WRITE_DOMAINS
from app.degraded.router import CommandRouter
from app.events.listener import EventListener
from app.events.runner import EventRunner
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.health import DOWN, UNKNOWN, ServiceHealth
from app.i18n import build_language_adapter
from app.i18n.store import LangOverlay
from app.memory.store import NoteStore
from app.needle.factory import build_fast_path_backend
from app.telemetry.setup import get_tracer, init_telemetry, shutdown_telemetry
from app.telemetry.snapshots import register_prompt, register_toolset
from app.telemetry.store import open_store
from app.tools import registry
from app.tools.adapter import LoopGuard, build_tools
from app.tools.context import ToolContext

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

log = logging.getLogger("agent")

# App loggers that should reach stdout. Uvicorn configures only its own
# loggers, so without this every agent.* INFO line (tool audit, event
# matches, firings) is silently dropped and only warnings get through.
_APP_LOGGERS = ("agent", "fast_path")


def _configure_app_logging() -> None:
    formatter = logging.Formatter("%(levelname)s:     %(name)s %(message)s")
    for name in _APP_LOGGERS:
        logger = logging.getLogger(name)
        if logger.handlers:
            continue  # idempotent: create_app may run more than once
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        if logger.level == logging.NOTSET:
            logger.setLevel(logging.INFO)


async def _teardown(rest, ws, audit=None, telemetry_provider=None, language=None) -> None:
    try:
        if language is not None:
            try:
                await language.aclose()
            except Exception:
                pass
        await rest.aclose()
    finally:
        try:
            if ws is not None:
                await ws.stop()
        finally:
            try:
                if audit is not None:
                    audit.close()
            finally:
                if telemetry_provider is not None:
                    shutdown_telemetry(telemetry_provider)



def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()
    _configure_app_logging()
    warn_if_llm_unset(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        write_domains = WRITE_DOMAINS if cfg.allow_actions else ()
        rest = RestClient(cfg.ha_base_url, cfg.ha_token,
                          allowed_write_domains=write_domains)
        audit = AuditSink(cfg.audit_db_path)
        ws: WebSocketClient | None = WebSocketClient(cfg.ws_url, cfg.ha_token)
        # Language adapter lives at the HTTP edge only; a no-op instance when
        # the layer is off, so the handlers never branch on config.
        language = build_language_adapter(cfg)
        app.state.language = language
        # Native-text overlay for /api/history. It lives in the
        # checkpoint DB, so it only exists when threads are durable — in-memory
        # threads do not survive the reload it exists to fix.
        overlay = None
        if cfg.language_layer_enabled and cfg.checkpoint_db_path:
            overlay = LangOverlay(cfg.checkpoint_db_path)
        app.state.lang_overlay = overlay
        # Memory notes share the checkpoint DB file (in-memory when it is unset).
        note_store = NoteStore(cfg.checkpoint_db_path)
        notes = note_store if note_store.available else None
        app.state.notes = notes

        # Nothing leaves the LAN: kill LangSmith env vars.
        os.environ.pop("LANGSMITH_TRACING", None)
        os.environ.pop("LANGCHAIN_TRACING_V2", None)

        # Initialise telemetry provider (None when disabled or DB unwritable).
        telemetry_provider = init_telemetry(cfg)
        app.state.telemetry = telemetry_provider

        # Open a second conn for snapshot registration and the labels write path.
        # (The exporter conn is owned by BatchSpanProcessor and is not reusable here.)
        snapshot_conn = None
        if telemetry_provider is not None and cfg.telemetry_db_path:
            try:
                snapshot_conn = open_store(cfg.telemetry_db_path)
            except Exception:
                log.warning("telemetry: could not open snapshot conn; skipping snapshot registration")

        # Expose the labels/snapshot conn on app.state so POST /api/labels can
        # write to the same DB.  None when telemetry is disabled or DB failed to open.
        app.state.telemetry_store = snapshot_conn

        health_client = None
        llm_health = None
        fp_health = None
        try:
            try:
                await ws.start(connect_timeout=cfg.ws_connect_timeout)
            except Exception as exc:
                # Keep the client: it reconnects in the background, so a slow HA
                # boot degrades the websocket tools (and event notes) only until
                # HA is up, not until the add-on restarts.
                log.warning("websocket not connected yet (%s) — retrying in the background", exc)
                ws.start_background()

            # Resolve "auto" llm_provider (and llama.cpp's real num_ctx) now
            # that we know the backend is reachable (or not). cfg stays the
            # unresolved closure variable from create_app; everything below
            # this point uses run_cfg.
            run_cfg = await resolve_llm_settings(cfg)

            # One client for every health probe; closed in `finally`.
            health_client = httpx.AsyncClient()
            unresolved = needs_reresolve(cfg, run_cfg)
            initial = DOWN if unresolved else UNKNOWN
            llm_health = ServiceHealth(
                "llm", build_llm_probe(run_cfg, health_client, unresolved), initial=initial,
            )
            app.state.llm_health = llm_health

            ctx = ToolContext(settings=run_cfg, rest=rest, ws=ws, audit=audit, notes=notes, lang=language)
            # Load the tool registry before building the fast path: it guards on
            # registry.get("trigger_action"), and build_agent (which also
            # loads the registry) runs later. load_all is idempotent.
            registry.load_all()
            enabled = await registry.resolve_enabled(ctx)
            app.state.enabled_tools = enabled
            fast_path = None
            try:
                fast_path = build_fast_path_backend(run_cfg, rest, ctx)
            except Exception:
                log.exception("fast path failed to build; running agent-only")

            if fast_path is not None:
                fp_health = ServiceHealth(
                    "fast_path", build_fast_path_probe(run_cfg.fast_path_url, health_client),
                )
            app.state.fp_health = fp_health

            # Build telemetry tuple for build_agent when enabled.
            telemetry_arg = None
            if telemetry_provider is not None:
                tracer = get_tracer()
                telemetry_arg = (tracer, snapshot_conn)

            async with open_checkpointer(run_cfg) as checkpointer:
                agent = build_agent(
                    run_cfg, ctx, checkpointer=checkpointer, fast_path=fast_path,
                    telemetry=telemetry_arg, enabled=enabled, fast_path_health=fp_health,
                )
                app.state.settings = run_cfg
                app.state.ctx = ctx
                app.state.rest = rest
                app.state.ws = ws
                app.state.agent = agent
                app.state.router = CommandRouter(
                    ctx, enabled, llm_health, fast_path=fast_path, fp_health=fp_health,
                )

                # Only an `auto` provider that could not be identified at boot
                # needs re-resolving; an explicit provider never changes. Once
                # resolved, the flag goes False for good.
                still_unresolved = [unresolved]

                async def _on_llm_recovered() -> None:
                    if not still_unresolved[0]:
                        return
                    fresh = await reresolve(cfg, app.state.settings)
                    if fresh is None:
                        return
                    log.info("llm: provider resolved to %s on recovery; rebuilding agent",
                             fresh.llm_provider)
                    app.state.agent = build_agent(
                        fresh, ctx, checkpointer=checkpointer, fast_path=fast_path,
                        telemetry=telemetry_arg, enabled=enabled, fast_path_health=fp_health,
                    )
                    app.state.settings = fresh
                    llm_health.set_probe(build_llm_probe(fresh, health_client))
                    still_unresolved[0] = False

                llm_health.on_recovery(_on_llm_recovered)
                llm_health.start()
                if fp_health is not None:
                    fp_health.start()

                # Event-triggered notes: the runner reads app.state.agent at fire
                # time, the listener subscribes now (or on the first connect).
                runner = EventRunner(lambda: app.state.agent, run_cfg, ctx)
                listener = EventListener(ws, notes, runner, run_cfg, rest)
                await listener.start()
                app.state.event_listener = listener

                # Register prompt + toolset snapshots now that the agent is built.
                if snapshot_conn is not None:
                    try:
                        base_prompt = build_system_prompt(run_cfg, ctx.skills_dir)
                        register_prompt(snapshot_conn, base_prompt)
                        guard = LoopGuard()
                        tools = build_tools(ctx, run_cfg.max_tier, guard=guard, enabled=enabled)
                        register_toolset(snapshot_conn, tools)
                    except Exception:
                        log.warning("telemetry: snapshot registration failed (non-fatal)", exc_info=True)

                yield
        finally:
            if llm_health is not None:
                await llm_health.stop()
            if fp_health is not None:
                await fp_health.stop()
            if health_client is not None:
                await health_client.aclose()
            if snapshot_conn is not None:
                try:
                    snapshot_conn.close()
                except Exception:
                    pass
            if overlay is not None:
                overlay.close()
            note_store.close()
            await _teardown(rest, ws, audit=audit, telemetry_provider=telemetry_provider,
                            language=language)

    app = FastAPI(lifespan=lifespan)

    app.include_router(chat_api.router)
    app.include_router(system_api.router)
    app.include_router(actions_api.router)
    app.include_router(notes_api.router)
    app.include_router(telemetry_api.router)

    # Mounted last so /api/* wins. Frontend must use relative fetch paths
    # ("api/chat", not "/api/chat") — HA ingress serves us under a prefix.
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    return app
