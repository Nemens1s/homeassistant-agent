"""/api/chat (Assist, non-streaming) and /api/chat/stream (web UI, SSE).

Both endpoints run the same turn: commands and LLM outages go to the
deterministic CommandRouter, everything else is translated in, run through
the agent and translated out. `ChatTurn` holds that shared sequence so the
two endpoints only differ in how they deliver the answer."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from langgraph.errors import GraphRecursionError
from opentelemetry import trace
from pydantic import BaseModel

from app.agent.history import close_dangling_turn, ran_action_this_turn
from app.agent.llm_health import is_llm_outage
from app.agent.middleware.telemetry_middleware import fast_path_seen
from app.agent.run_scope import scope_configurable
from app.agent.streaming import stream_events
from app.degraded import Reply
from app.degraded.router import CommandRouter, offline_message
from app.i18n import set_language_attributes
from app.telemetry import conventions as C
from app.telemetry.setup import get_tracer

log = logging.getLogger("agent")

router = APIRouter()


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
    # True when answered without the LLM (command or degraded mode); the reply is English.
    degraded: bool = False


def _set_span(span, **attributes) -> None:
    """Telemetry is best-effort: a failing span never fails the request."""
    try:
        for key, value in attributes.items():
            span.set_attribute(key, value)
    except Exception:
        pass


# Shown when the agent fails for a reason other than an LLM outage (a bug).
# Assist speaks it instead of playing a generic error tone.
_INTERNAL_ERROR = "Sorry, something went wrong on my side. Please try again."


def _recursion_message(limit: int) -> str:
    return (f"I stopped after {limit} tool steps without reaching an answer. "
            "Try a more specific question.")


class ChatTurn:
    """One chat turn: routing, translation, and the bookkeeping after the
    agent answers. The endpoint drives the agent call itself."""

    def __init__(self, state, req: ChatRequest, span, endpoint: str, channel: str) -> None:
        self._state = state
        self._req = req
        self._span = span
        self._endpoint = endpoint
        self._channel = channel
        self._language = state.language
        self.inbound = None
        trace_id = span.get_span_context().trace_id
        self.request_id = format(trace_id, "032x") if trace_id else None

    def wants_router(self) -> bool:
        """Commands, and every message while the LLM is down, are answered
        deterministically — before lang-mt, which may share the LLM's host."""
        if CommandRouter.is_command(self._req.message):
            return True
        llm_health = getattr(self._state, "llm_health", None)
        return llm_health is not None and llm_health.is_down

    async def router_reply(self) -> Reply:
        """The router's answer, or the generic offline reply if it fails —
        a router bug must never turn into a 500."""
        llm_health = getattr(self._state, "llm_health", None)
        try:
            reply = await self._state.router.handle(self._req.message, self._req.thread_id)
        except Exception:
            log.exception("degraded: router failed")
            reply = None
        if reply is None:
            reply = Reply(offline_message(llm_health), "degraded")
        _set_span(self._span, **{C.GOSLING_PATH: reply.kind})
        return reply

    async def translate_in(self) -> str:
        """Translate the user's message; everything after this is English."""
        self.inbound = await self._language.inbound(self._req.message, self._req.thread_id)
        set_language_attributes(self._span, self._language, self.inbound)
        # gosling.input.text is the English text the agent saw; the original
        # lives under gosling.lang.original_text.
        _set_span(self._span, **{
            C.GOSLING_INPUT_TEXT: self.inbound.english_text,
            C.GOSLING_ENDPOINT: self._endpoint,
            C.GOSLING_CHANNEL: self._channel,
        })
        return self.inbound.english_text

    def configurable(self) -> dict:
        return scope_configurable(self.inbound.language)

    async def finish(self, reply_en: str) -> tuple[str, str | None]:
        """Translate the agent's English answer back. Returns the reply and
        the English text (None when nothing was translated)."""
        reply = await self._language.outbound(reply_en, self.inbound)
        set_language_attributes(self._span, self._language, self.inbound, reply_native=reply)
        self._record_native_turn(reply_en, reply)
        path = "fast_path" if fast_path_seen.get() else "agent"
        _set_span(self._span, **{C.GOSLING_OUTPUT_TEXT: reply_en, C.GOSLING_PATH: path})
        if reply == reply_en:
            return reply, None
        return reply, reply_en

    def _record_native_turn(self, reply_en: str, reply: str) -> None:
        """Keep the native texts for /api/history; English stays in the graph."""
        overlay = getattr(self._state, "lang_overlay", None)
        inbound = self.inbound
        if overlay is None or inbound.language == "en":
            return
        overlay.record(
            self._req.thread_id,
            language=inbound.language,
            original_text=inbound.original_text,
            english_text=inbound.english_text,
            reply_en=reply_en,
            reply_native=reply if reply != reply_en else None,
        )

    async def after_outage(self, exc: Exception) -> Reply:
        """The LLM died mid-turn: mark it down and answer in degraded mode.
        Uses the original text on purpose: action names may be non-English,
        and lang-mt may share the LLM's machine."""
        llm_health = getattr(self._state, "llm_health", None)
        if llm_health is not None:
            llm_health.mark_down(exc)
        agent = self._state.agent
        thread_id = self._req.thread_id
        # An action may already have run before the model call that failed;
        # re-running the same text through the router could fire it twice.
        if await ran_action_this_turn(agent, thread_id):
            text = ("I started on that, but my language model went offline before I "
                    "could finish. Check the result before trying again. "
                    + offline_message(llm_health))
            return Reply(text, "degraded")
        await close_dangling_turn(agent, thread_id)
        return await self.router_reply()


@router.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    log.info("chat thread=%s len=%d", req.thread_id, len(req.message))
    state = request.app.state
    tracer = get_tracer()

    with tracer.start_as_current_span(C.SPAN_INVOKE_AGENT) as span:
        turn = ChatTurn(state, req, span, "/api/chat", req.channel or "assist")
        if turn.wants_router():
            reply = await turn.router_reply()
            _set_span(span, **{C.GOSLING_OUTCOME: "ok"})
            return ChatResponse(reply=reply.text, degraded=True, language="en")

        english = await turn.translate_in()
        configurable = {"thread_id": req.thread_id}
        configurable.update(turn.configurable())
        try:
            result = await state.agent.ainvoke(
                {"messages": [{"role": "user", "content": english}]},
                config={
                    "configurable": configurable,
                    "recursion_limit": state.settings.recursion_limit,
                },
            )
            reply, reply_en = await turn.finish(result["messages"][-1].content)
            _set_span(span, **{C.GOSLING_OUTCOME: "ok"})
            return ChatResponse(
                reply=reply,
                request_id=turn.request_id,
                language=turn.inbound.language,
                reply_en=reply_en,
            )

        except GraphRecursionError:
            _set_span(span, **{C.GOSLING_OUTCOME: "recursion_limit", C.GOSLING_PATH: "agent"})
            return ChatResponse(
                reply=_recursion_message(state.settings.recursion_limit),
                request_id=turn.request_id,
            )

        except Exception as exc:
            if is_llm_outage(exc):
                reply = await turn.after_outage(exc)
                _set_span(span, **{C.GOSLING_OUTCOME: "llm_unavailable"})
                return ChatResponse(reply=reply.text, request_id=turn.request_id,
                                    degraded=True, language="en")
            log.exception("chat: agent run failed")
            await close_dangling_turn(state.agent, req.thread_id)
            _set_span(span, **{C.GOSLING_OUTCOME: "error", C.GOSLING_PATH: "agent"})
            return ChatResponse(reply=_INTERNAL_ERROR, request_id=turn.request_id)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def _reply_frames(reply: Reply, request_id) -> list[str]:
    frames = []
    for call in reply.tool_calls:
        frames.append(_sse({"type": "tool_call", "name": call["name"], "args": {}}))
        frames.append(_sse({"type": "tool_result", "name": call["name"],
                            "status": call["status"]}))
    frames.append(_sse({"type": "done", "reply": reply.text, "reply_en": None,
                        "language": "en", "degraded": True, "request_id": request_id}))
    return frames


@router.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    log.info("chat/stream thread=%s len=%d", req.thread_id, len(req.message))
    state = request.app.state
    tracer = get_tracer()

    async def sse() -> AsyncIterator[str]:
        outcome = "ok"
        span = tracer.start_span(C.SPAN_INVOKE_AGENT)
        # Make the root span CURRENT across the whole stream: child spans
        # created inside the agent graph (chat, execute_tool,
        # fast_path.classify) must parent onto it and share its trace_id.
        # Without this they become orphan traces → phantom empty `requests`
        # rows. Keep the manual .end() in `finally` (end_on_exit=False).
        try:
            with trace.use_span(span, end_on_exit=False):
                turn = ChatTurn(state, req, span, "/api/chat/stream", req.channel or "ui")
                if turn.wants_router():
                    reply = await turn.router_reply()
                    for frame in _reply_frames(reply, turn.request_id):
                        yield frame
                    return

                english = await turn.translate_in()
                # Fast path is handled transparently by FastPathMiddleware inside the agent.
                async for event in stream_events(
                    state.agent,
                    english,
                    req.thread_id,
                    state.settings.recursion_limit,
                    extra_configurable=turn.configurable(),
                ):
                    kind = event.get("type")
                    if kind == "done":
                        # Tokens streamed in English (they cannot be
                        # translated incrementally); `done` carries the
                        # native reply the frontend swaps in.
                        reply, reply_en = await turn.finish(event.get("reply", ""))
                        yield _sse({
                            **event,
                            "reply": reply,
                            "reply_en": reply_en,
                            "language": turn.inbound.language,
                            "request_id": turn.request_id,
                        })
                    elif kind == "error":
                        outcome = "error"
                        yield _sse({**event, "request_id": turn.request_id})
                    elif kind == "llm_unavailable":
                        # Partial tokens already sent are overwritten by
                        # the frontend with the `done` text.
                        outcome = "llm_unavailable"
                        reply = await turn.after_outage(ConnectionError(event.get("error", "")))
                        for frame in _reply_frames(reply, turn.request_id):
                            yield frame
                    else:
                        yield _sse(event)

        except GeneratorExit:
            outcome = "cancelled"
        except Exception:
            # End the stream with an explanation instead of cutting it: the
            # browser would only show a network error.
            log.exception("chat/stream: agent run failed")
            await close_dangling_turn(state.agent, req.thread_id)
            outcome = "error"
            yield _sse({"type": "error", "message": _INTERNAL_ERROR})
        finally:
            _set_span(span, **{C.GOSLING_OUTCOME: outcome})
            span.end()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
