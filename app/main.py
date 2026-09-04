"""Add-on entrypoint: FastAPI app factory. Run with
`uvicorn app.main:create_app --factory` — the factory keeps construction
config-injected and testable (no import-time singletons)."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

import httpx
from fastapi import FastAPI, HTTPException, Header
from langgraph.errors import GraphRecursionError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.checkpointer import open_checkpointer
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
"""
OpenAI models are required for local testing of voice pipeline until this app isn't wired up to the HA
These can be cleared up later once I get there
"""
class OAIMessage(BaseModel):
    role: str
    content: str


class OAIChatRequest(BaseModel):
    model: str = "local-agent"
    messages: list[OAIMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    # HA may send other fields (stream, etc.) — ignored


class OAIChoice(BaseModel):
    index: int = 0
    message: OAIMessage
    finish_reason: str = "stop"


class OAIUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OAIChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[OAIChoice]
    usage: OAIUsage = OAIUsage()

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

    """
    These endpoints are only meant for Voice pipeline during testing
    """
    @app.post("/v1/chat/completions", response_model=OAIChatResponse)
    async def openai_chat_completions(req: OAIChatRequest, authorization: str | None = Header(default=None)):
        # Extract the last user message — the agent manages its own history
        # and system prompt via MemorySaver, so we ignore HA's conversation
        # history and system prompt here.
        user_msg = ""
        for msg in reversed(req.messages):
            if msg.role == "user":
                user_msg = msg.content
                break

        if not user_msg:
            raise HTTPException(status_code=400, detail="No user message found")

        # Use a fixed thread_id for voice pipeline; the agent's MemorySaver
        # handles conversation continuity within a session. For multi-user or
        # multi-pipeline scenarios, derive thread_id from something else.

        router = getattr(app.state, "fast_path", None)
        response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        if router is not None:
            reply = await router.try_fast_path(user_msg, thread_id=f"voice-{uuid.uuid4().hex[:8]}")
            if reply is not None:
                # return ChatResponse(reply=reply)
                return OAIChatResponse(
                    id=response_id,
                    created=int(time.time()),
                    model=req.model,
                    choices=[
                        OAIChoice(
                            message=OAIMessage(role="assistant", content=reply),
                        )
                    ],
                )
        try:
            reply = await app.state.agent.ainvoke(
                {"messages": [{"role": "user", "content": user_msg}]},
                config={
                    "configurable": {"thread_id": f"voice-{uuid.uuid4().hex[:8]}"},
                    "recursion_limit": app.state.settings.recursion_limit,
                },
            )
            return OAIChatResponse(
                id=response_id,
                created=int(time.time()),
                model=req.model,
                choices=[
                    OAIChoice(
                        message=OAIMessage(role="assistant", content=reply["messages"][-1].content),
                    )
                ],
            )
        except GraphRecursionError:
            return OAIChatResponse(
                id=response_id,
                created=int(time.time()),
                model=req.model,
                choices=[
                    OAIChoice(
                        message=OAIMessage(role="assistant", content=f"I stopped after {app.state.settings.recursion_limit} tool steps without reaching an answer. Try a more specific question."),
                    )
                ],
            )



    # HA's OpenAI integration may probe /v1/models on setup
    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {
                    "id": "local-agent",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "local",
                }
            ],
        }

    # Mounted last so /api/* wins. Frontend must use relative fetch paths
    # ("api/chat", not "/api/chat") — HA ingress serves us under a prefix.
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
    return app
