"""Add-on entrypoint: FastAPI app factory. Run with
`uvicorn app.main:create_app --factory` — the factory keeps construction
config-injected and testable (no import-time singletons)."""

from __future__ import annotations

import json
import logging
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
from app.agent.streaming import stream_events
from app.agent.factory import build_agent
from app.audit import AuditSink
from app.needle.factory import build_fast_path_router
from app.config import Settings, load_settings
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.tools.context import ToolContext

log = logging.getLogger("agent")


async def _teardown(rest, ws, audit=None) -> None:
    try:
        await rest.aclose()
    finally:
        try:
            if ws is not None:
                await ws.stop()
        finally:
            if audit is not None:
                audit.close()


# ---------- models ----------
class ChatRequest(BaseModel):
    message: str
    thread_id: str = "default"


class ChatResponse(BaseModel):
    reply: str


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        write_domains = (
            tuple(cfg.allowed_domains) if cfg.max_tier >= 2 else ()
        )
        rest = RestClient(cfg.ha_base_url, cfg.ha_token,
                          allowed_write_domains=write_domains)
        audit = AuditSink(cfg.audit_db_path)
        ws: WebSocketClient | None = WebSocketClient(cfg.ws_url, cfg.ha_token)
        try:
            try:
                await ws.start(connect_timeout=cfg.ws_connect_timeout)
            except Exception as exc:
                log.warning("websocket unavailable (%s) — area/automation tools degraded", exc)
                ws = None
            ctx = ToolContext(settings=cfg, rest=rest, ws=ws, audit=audit)
            async with open_checkpointer(cfg) as checkpointer:
                app.state.settings = cfg
                app.state.rest = rest
                app.state.ws = ws
                app.state.agent = build_agent(cfg, ctx, checkpointer=checkpointer)
                app.state.fast_path = None
                try:
                    app.state.fast_path = build_fast_path_router(cfg, rest, ctx)
                except Exception:
                    log.exception("needle fast path failed to build; running agent-only")
                yield
        finally:
            await _teardown(rest, ws, audit=audit)

    app = FastAPI(lifespan=lifespan)

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        log.info("chat thread=%s len=%d", req.thread_id, len(req.message))
        router = getattr(app.state, "fast_path", None)
        if router is not None:
            reply = await router.try_fast_path(req.message, req.thread_id)
            if reply is not None:
                return ChatResponse(reply=reply)
        try:
            result = await app.state.agent.ainvoke(
                {"messages": [{"role": "user", "content": req.message}]},
                config={
                    "configurable": {"thread_id": req.thread_id},
                    "recursion_limit": app.state.settings.recursion_limit,
                },
            )
            return ChatResponse(reply=result["messages"][-1].content)
        except GraphRecursionError:
            return ChatResponse(
                reply=f"I stopped after {app.state.settings.recursion_limit} tool steps without reaching an answer. Try a more specific question."
            )

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        log.info("chat/stream thread=%s len=%d", req.thread_id, len(req.message))

        def _sse(event: dict) -> str:
            return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"

        async def sse() -> AsyncIterator[str]:
            # Mirror /api/chat: try the Needle fast-path first. A hit means no
            # LLM ran, so there is nothing to stream — emit token + done.
            router = getattr(app.state, "fast_path", None)
            if router is not None:
                reply = await router.try_fast_path(req.message, req.thread_id)
                if reply is not None:
                    yield _sse({"type": "token", "text": reply})
                    yield _sse({"type": "done", "reply": reply})
                    return
            async for event in stream_events(
                app.state.agent,
                req.message,
                req.thread_id,
                app.state.settings.recursion_limit,
            ):
                yield _sse(event)

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

    # Mounted last so /api/* wins. Frontend must use relative fetch paths
    # ("api/chat", not "/api/chat") — HA ingress serves us under a prefix.
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    return app
