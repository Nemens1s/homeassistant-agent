"""Add-on entrypoint: FastAPI app factory. Run with
`uvicorn app.main:create_app --factory` — the factory keeps construction
config-injected and testable (no import-time singletons)."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langgraph.errors import GraphRecursionError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.checkpointer import open_checkpointer
from app.agent.run_scope import scope_configurable
from app.agent.streaming import stream_events
from app.agent.factory import build_agent
from app.audit import AuditSink
from app.needle.factory import build_fast_path_backend
from app.config import Settings, load_settings
from app.i18n import build_language_adapter, set_language_attributes
from app.i18n.store import LangOverlay, apply_overlay
from app.ha.rest import RestClient
from app.events.listener import EventListener
from app.events.runner import EventRunner
from app.memory.store import NoteStore
from app.ha.websocket import WebSocketClient, ws_is_ready
from app.tools.context import ToolContext

log = logging.getLogger("agent")


def _assert_otlp_is_lan(endpoint: str) -> None:
    """Refuse to start if the OTLP endpoint resolves to a public IP.

    Parses the hostname from *endpoint* (a URL such as
    ``http://otelcol.lan:4318``), resolves all of its addresses, and raises
    ``RuntimeError`` if ANY resolved address is not an RFC-1918 private address
    or loopback.  This is a hard guard — nothing leaves the LAN.
    """
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.hostname
    if not host:
        raise RuntimeError(
            f"otlp_endpoint {endpoint!r}: cannot parse hostname — "
            "must be a full URL like http://host:4318"
        )

    try:
        results = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise RuntimeError(
            f"otlp_endpoint {endpoint!r}: failed to resolve {host!r}: {exc}"
        ) from exc

    for _family, _type, _proto, _canonname, sockaddr in results:
        addr_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        if not (addr.is_private or addr.is_loopback):
            raise RuntimeError(
                f"otlp_endpoint {endpoint!r} resolved to public address "
                f"{addr_str} — only LAN/loopback endpoints are allowed"
            )


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
                    from app.telemetry.setup import shutdown_telemetry
                    shutdown_telemetry(telemetry_provider)


# ---------- models ----------
class ChatRequest(BaseModel):
    model_config = {"extra": "ignore"}

    message: str
    thread_id: str = "default"
    channel: str | None = None


class ChatResponse(BaseModel):
    reply: str
    request_id: str | None = None
    # Language layer: the language the reply is in, and the English
    # text the agent actually produced. reply_en is None when nothing was
    # translated. Gosling ignores unknown fields, so this is additive.
    language: str | None = None
    reply_en: str | None = None


class LabelRequest(BaseModel):
    request_id: str
    source: str
    rating: int | None = None
    correct_tool: str | None = None
    correct_entity_id: str | None = None
    note: str | None = None


class ToggleRequest(BaseModel):
    enabled: bool


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.telemetry.setup import init_telemetry, get_tracer
        from app.telemetry.snapshots import register_prompt, register_toolset
        from app.telemetry.store import open_store
        from app.agent.factory import build_system_prompt
        from app.skills import list_skills

        write_domains = (
            tuple(cfg.allowed_domains) if cfg.max_tier >= 2 else ()
        )
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

        # Nothing leaves the LAN: kill LangSmith env vars and enforce a
        # private-address-only constraint on the OTLP endpoint.
        os.environ.pop("LANGSMITH_TRACING", None)
        os.environ.pop("LANGCHAIN_TRACING_V2", None)
        if cfg.otlp_endpoint:
            _assert_otlp_is_lan(cfg.otlp_endpoint)

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

        try:
            try:
                await ws.start(connect_timeout=cfg.ws_connect_timeout)
            except Exception as exc:
                # Keep the client: it reconnects in the background, so a slow HA
                # boot degrades the websocket tools (and event notes) only until
                # HA is up, not until the add-on restarts.
                log.warning("websocket not connected yet (%s) — retrying in the background", exc)
                ws.start_background()

            ctx = ToolContext(settings=cfg, rest=rest, ws=ws, audit=audit, notes=notes, lang=language)
            # Load the tool registry before building the fast path: it guards on
            # registry.get("trigger_action"), and build_agent (which also
            # loads the registry) runs later. load_all is idempotent.
            from app.tools import registry
            registry.load_all()
            fast_path = None
            try:
                fast_path = build_fast_path_backend(cfg, rest, ctx)
            except Exception:
                log.exception("needle fast path failed to build; running agent-only")

            # Build telemetry tuple for build_agent when enabled.
            telemetry_arg = None
            if telemetry_provider is not None:
                tracer = get_tracer()
                telemetry_arg = (tracer, snapshot_conn)

            async with open_checkpointer(cfg) as checkpointer:
                agent = build_agent(
                    cfg, ctx, checkpointer=checkpointer, fast_path=fast_path,
                    telemetry=telemetry_arg,
                )
                app.state.settings = cfg
                app.state.rest = rest
                app.state.ws = ws
                app.state.agent = agent

                # Event-triggered notes: the runner reads app.state.agent at fire
                # time, the listener subscribes now (or on the first connect).
                runner = EventRunner(lambda: app.state.agent, cfg)
                listener = EventListener(ws, notes, runner, cfg)
                await listener.start()
                app.state.event_listener = listener

                # Register prompt + toolset snapshots now that the agent is built.
                if snapshot_conn is not None:
                    try:
                        from app.tools.adapter import build_tools, LoopGuard
                        base_prompt = build_system_prompt(cfg, ctx.skills_dir)
                        register_prompt(snapshot_conn, base_prompt)
                        guard = LoopGuard()
                        tools = build_tools(ctx, cfg.max_tier, guard=guard)
                        register_toolset(snapshot_conn, tools)
                    except Exception:
                        log.warning("telemetry: snapshot registration failed (non-fatal)", exc_info=True)

                yield
        finally:
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

    def _record_native_turn(thread_id, inbound, reply_en, reply) -> None:
        """Keep the native texts for /api/history; English stays in the graph."""
        overlay = getattr(app.state, "lang_overlay", None)
        if overlay is None or inbound.language == "en":
            return
        overlay.record(
            thread_id,
            language=inbound.language,
            original_text=inbound.original_text,
            english_text=inbound.english_text,
            reply_en=reply_en,
            reply_native=reply if reply != reply_en else None,
        )

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        log.info("chat thread=%s len=%d", req.thread_id, len(req.message))
        from app.telemetry.setup import get_tracer
        from app.telemetry import conventions as C
        from app.agent.middleware.telemetry_middleware import fast_path_seen as _fp_seen

        channel = req.channel or "assist"
        tracer = get_tracer()
        request_id: str | None = None

        with tracer.start_as_current_span(C.SPAN_INVOKE_AGENT) as span:
            # Translate on the way in; everything past this point is English.
            language = app.state.language
            inbound = await language.inbound(req.message, req.thread_id)
            set_language_attributes(span, language, inbound)

            # Set input attributes on the root span. gosling.input.text is the
            # English text the agent saw; the original lives under
            # gosling.lang.original_text.
            try:
                span.set_attribute(C.GOSLING_INPUT_TEXT, inbound.english_text)
                span.set_attribute(C.GOSLING_ENDPOINT, "/api/chat")
                span.set_attribute(C.GOSLING_CHANNEL, channel)
            except Exception:
                pass

            trace_id = span.get_span_context().trace_id
            request_id = format(trace_id, "032x") if trace_id else None

            try:
                configurable = {"thread_id": req.thread_id}
                configurable.update(scope_configurable(inbound.language))
                result = await app.state.agent.ainvoke(
                    {"messages": [{"role": "user", "content": inbound.english_text}]},
                    config={
                        "configurable": configurable,
                        "recursion_limit": app.state.settings.recursion_limit,
                    },
                )
                reply_en = result["messages"][-1].content
                reply = await language.outbound(reply_en, inbound)
                set_language_attributes(span, language, inbound, reply_native=reply)
                _record_native_turn(req.thread_id, inbound, reply_en, reply)

                try:
                    span.set_attribute(C.GOSLING_OUTPUT_TEXT, reply_en)
                    path = "fast_path" if _fp_seen.get() else "agent"
                    span.set_attribute(C.GOSLING_PATH, path)
                    span.set_attribute(C.GOSLING_OUTCOME, "ok")
                except Exception:
                    pass

                return ChatResponse(
                    reply=reply,
                    request_id=request_id,
                    language=inbound.language,
                    reply_en=reply_en if reply != reply_en else None,
                )

            except GraphRecursionError:
                try:
                    span.set_attribute(C.GOSLING_OUTCOME, "recursion_limit")
                    span.set_attribute(C.GOSLING_PATH, "agent")
                except Exception:
                    pass
                return ChatResponse(
                    reply=f"I stopped after {app.state.settings.recursion_limit} tool steps without reaching an answer. Try a more specific question.",
                    request_id=request_id,
                )

            except Exception:
                try:
                    span.set_attribute(C.GOSLING_OUTCOME, "error")
                    span.set_attribute(C.GOSLING_PATH, "agent")
                except Exception:
                    pass
                raise

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        log.info("chat/stream thread=%s len=%d", req.thread_id, len(req.message))
        from opentelemetry import trace
        from app.telemetry.setup import get_tracer
        from app.telemetry import conventions as C
        from app.agent.middleware.telemetry_middleware import fast_path_seen as _fp_seen

        channel = req.channel or "ui"
        tracer = get_tracer()

        def _sse(event: dict) -> str:
            return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"

        async def sse() -> AsyncIterator[str]:
            span = tracer.start_span(C.SPAN_INVOKE_AGENT)
            request_id: str | None = None
            # Make the root span CURRENT across the whole stream: child spans
            # created inside the agent graph (chat, execute_tool,
            # fast_path.classify) must parent onto it and share its trace_id.
            # Without this they become orphan traces → phantom empty `requests`
            # rows. Keep the manual .end() in `finally` (end_on_exit=False).
            try:
                with trace.use_span(span, end_on_exit=False):
                    language = app.state.language
                    inbound = await language.inbound(req.message, req.thread_id)
                    set_language_attributes(span, language, inbound)
                    try:
                        span.set_attribute(C.GOSLING_INPUT_TEXT, inbound.english_text)
                        span.set_attribute(C.GOSLING_ENDPOINT, "/api/chat/stream")
                        span.set_attribute(C.GOSLING_CHANNEL, channel)
                    except Exception:
                        pass

                    trace_id = span.get_span_context().trace_id
                    request_id = format(trace_id, "032x") if trace_id else None
                    outcome = "ok"

                    # Fast path is handled transparently by FastPathMiddleware inside the agent.
                    async for event in stream_events(
                        app.state.agent,
                        inbound.english_text,
                        req.thread_id,
                        app.state.settings.recursion_limit,
                        extra_configurable=scope_configurable(inbound.language),
                    ):
                        if event.get("type") == "done":
                            # Tokens streamed in English (they cannot be
                            # translated incrementally); `done` carries the
                            # native reply the frontend swaps in.
                            reply_en = event.get("reply", "")
                            reply = await language.outbound(reply_en, inbound)
                            set_language_attributes(span, language, inbound,
                                                    reply_native=reply)
                            _record_native_turn(req.thread_id, inbound, reply_en, reply)
                            try:
                                span.set_attribute(C.GOSLING_OUTPUT_TEXT, reply_en)
                                path = "fast_path" if _fp_seen.get() else "agent"
                                span.set_attribute(C.GOSLING_PATH, path)
                            except Exception:
                                pass
                            yield _sse({
                                **event,
                                "reply": reply,
                                "reply_en": reply_en if reply != reply_en else None,
                                "language": inbound.language,
                                "request_id": request_id,
                            })
                        elif event.get("type") == "error":
                            outcome = "error"
                            yield _sse({**event, "request_id": request_id})
                        else:
                            yield _sse(event)

            except GeneratorExit:
                outcome = "cancelled"
            except Exception:
                outcome = "error"
                raise
            finally:
                try:
                    span.set_attribute(C.GOSLING_OUTCOME, outcome)
                except Exception:
                    pass
                span.end()

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/health")
    async def health() -> dict:
        ha_ok = False
        llm_ok = False
        try:
            ha_ok = await app.state.rest.ping()
        except Exception:
            pass
        try:
            s = app.state.settings
            if s.llm_provider == "ollama":
                health_url = f"{s.llm_url}/api/version"
            else:
                health_url = f"{s.llm_url}/health"
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(health_url)
                llm_ok = resp.status_code == 200
        except Exception:
            pass
        ws_ok = app.state.ws is not None and app.state.ws.connected
        return {
            "status": "ok" if (ha_ok and llm_ok) else "degraded",
            "ha": ha_ok,
            "llm": llm_ok,
            "websocket": ws_ok,
        }

    @app.get("/api/history")
    async def get_history(thread_id: str) -> dict:
        from app.agent.history import display_transcript

        snap = await app.state.agent.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        messages = (snap.values or {}).get("messages", [])
        turns = display_transcript(messages)
        overlay = getattr(app.state, "lang_overlay", None)
        if overlay is not None:
            turns = apply_overlay(turns, overlay.rows_for(thread_id))
        return {"messages": turns}

    @app.get("/api/telemetry/summary")
    async def get_telemetry_summary(days: int = 7) -> dict:
        from fastapi import HTTPException
        from app.telemetry.store import summary as store_summary

        conn = getattr(app.state, "telemetry_store", None)
        if conn is None:
            raise HTTPException(status_code=503, detail="telemetry disabled")
        return store_summary(conn, days=days)

    @app.get("/api/telemetry/requests")
    async def get_telemetry_requests(limit: int = 20, cursor: str | None = None) -> dict:
        from fastapi import HTTPException
        from app.telemetry.store import recent_requests as store_recent_requests

        conn = getattr(app.state, "telemetry_store", None)
        if conn is None:
            raise HTTPException(status_code=503, detail="telemetry disabled")
        return store_recent_requests(conn, limit=limit, cursor=cursor)

    @app.post("/api/labels")
    async def post_label(req: LabelRequest) -> dict:
        from fastapi import HTTPException
        from app.telemetry.store import insert_label

        conn = getattr(app.state, "telemetry_store", None)
        if conn is None:
            raise HTTPException(status_code=503, detail="telemetry disabled")
        label_id = insert_label(
            conn,
            request_id=req.request_id,
            source=req.source,
            rating=req.rating,
            correct_tool=req.correct_tool,
            correct_entity_id=req.correct_entity_id,
            note=req.note,
        )
        return {"id": label_id}

    @app.get("/api/actions")
    async def get_actions() -> dict:
        from fastapi import HTTPException
        from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX

        ws = app.state.ws
        if not ws_is_ready(ws):
            raise HTTPException(status_code=503, detail="websocket unavailable")

        registry_entries = await ws.request("config/entity_registry/list")
        states = await app.state.rest.list_states()
        state_map = {s["entity_id"]: s for s in states}

        actions = []
        for entry in registry_entries:
            entity_id = entry["entity_id"]
            if not entity_id.startswith((AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX)):
                continue

            disabled_by = entry.get("disabled_by")
            is_automation = entity_id.startswith("automation.")

            if is_automation:
                s = state_map.get(entity_id)
                enabled = (not disabled_by) and s is not None and s["state"] == "on"
            else:
                enabled = disabled_by is None

            s = state_map.get(entity_id)
            if s:
                name = s.get("attributes", {}).get("friendly_name") or ""
            else:
                name = entry.get("name") or entry.get("original_name") or entity_id

            actions.append({
                "entity_id": entity_id,
                "name": name,
                "type": "automation" if is_automation else "script",
                "enabled": enabled,
            })

        return {"actions": actions}

    @app.post("/api/actions/{entity_id:path}/toggle")
    async def toggle_action(entity_id: str, req: ToggleRequest) -> dict:
        from fastapi import HTTPException
        from app.constants import AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX

        if not entity_id.startswith((AI_AUTOMATION_PREFIX, AI_SCRIPT_PREFIX)):
            raise HTTPException(status_code=403, detail="not an AI-controlled entity")

        ws = app.state.ws
        if not ws_is_ready(ws):
            raise HTTPException(status_code=503, detail="websocket unavailable")

        if entity_id.startswith("automation."):
            service = "turn_on" if req.enabled else "turn_off"
            await ws.management_request(
                "call_service",
                domain="automation",
                service=service,
                service_data={"entity_id": entity_id},
            )
        else:
            await ws.management_request(
                "config/entity_registry/update",
                entity_id=entity_id,
                disabled_by=None if req.enabled else "user",
            )
            if req.enabled:
                await ws.management_request("call_service", domain="script", service="reload")

        return {"ok": True}

    # Mounted last so /api/* wins. Frontend must use relative fetch paths
    # ("api/chat", not "/api/chat") — HA ingress serves us under a prefix.
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    return app
