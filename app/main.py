"""Add-on entrypoint: FastAPI app factory. Run with
`uvicorn app.main:create_app --factory` — the factory keeps construction
config-injected and testable (no import-time singletons)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.factory import build_agent
from app.config import Settings, load_settings
from app.ha.rest import RestClient
from app.ha.websocket import WebSocketClient
from app.tools.context import ToolContext

log = logging.getLogger("agent")


class ChatRequest(BaseModel):
    message: str
    thread_id: str = "default"


class ChatResponse(BaseModel):
    reply: str


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rest = RestClient(cfg.ha_base_url, cfg.ha_token)
        ws: WebSocketClient | None = WebSocketClient(cfg.ws_url, cfg.ha_token)
        try:
            await ws.start(connect_timeout=cfg.ws_connect_timeout)
        except Exception as exc:
            log.warning("websocket unavailable (%s) — area/automation tools degraded", exc)
            ws = None
        ctx = ToolContext(settings=cfg, rest=rest, ws=ws)
        app.state.settings = cfg
        app.state.rest = rest
        app.state.ws = ws
        app.state.agent = build_agent(cfg, ctx)
        yield
        await rest.aclose()
        if ws is not None:
            await ws.stop()

    app = FastAPI(lifespan=lifespan)

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        result = await app.state.agent.ainvoke(
            {"messages": [{"role": "user", "content": req.message}]},
            config={
                "configurable": {"thread_id": req.thread_id},
                "recursion_limit": app.state.settings.recursion_limit,
            },
        )
        return ChatResponse(reply=result["messages"][-1].content)

    @app.get("/api/health")
    async def health() -> dict:
        ha_ok = False
        ollama_ok = False
        try:
            ha_ok = await app.state.rest.ping()
        except Exception:
            pass
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{app.state.settings.ollama_url}/api/version")
                ollama_ok = resp.status_code == 200
        except Exception:
            pass
        ws_ok = app.state.ws is not None and app.state.ws.connected
        return {
            "status": "ok" if (ha_ok and ollama_ok) else "degraded",
            "ha": ha_ok,
            "ollama": ollama_ok,
            "websocket": ws_ok,
        }

    # Mounted last so /api/* wins. Frontend must use relative fetch paths
    # ("api/chat", not "/api/chat") — HA ingress serves us under a prefix.
    app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
    return app
