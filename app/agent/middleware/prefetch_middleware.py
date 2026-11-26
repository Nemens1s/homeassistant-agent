"""Pre-fetch for single-intent reads.

A read question costs two model calls: one to pick the tool, one to phrase
the result. The first is where small models fail. When the right tool is a
plain keyword or entity_id match, this runs it BEFORE the first model call:
a synthetic tool call the agent's tool node executes (adapter audit, envelope
and loop guard apply), so the model's only job is to phrase the result.

A wrong pre-fetch is acceptable: the model keeps its (subsetted) menu and can
still call the right tool, so a miss costs one extra tool result in the
prompt, never a wrong answer. Read answers are never templated.

Sits inside FastPath (Needle sees the raw message) and outside
WatchedEntities (the decision sees the text before <home_context>).
Never fails a request: any error falls through to the model."""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.middleware.fast_path_middleware import (
    has_coordination_cue,
    has_deferral_cue,
    opted_out,
)
from app.agent.middleware.tool_subset_middleware import latest_human_text
from app.agent.tool_router import _KEYWORDS, _ROOM_OVERVIEW, CORE_TOOLS
from app.ha.websocket import ws_is_ready
from app.telemetry import conventions
from app.tools.helpers.request_context import names_a_room, room_terms

log = logging.getLogger("prefetch")

PREFETCH_PREFIX = "prefetch-"
# The "model" of the synthetic response; telemetry counts the path by it.
PREFETCH_MODEL = conventions.PREFETCH_MODEL

_ENTITY_ID = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")

# Tools that answer with no arguments, so a keyword hit is enough to run one.
ZERO_ARG_TOOLS: frozenset[str] = frozenset({
    "get_weather",
    "get_battery_status",
    "get_person_locations",
    "get_vacuum_state",
    "get_error_log",
    "list_scheduled",
    "list_actions",
})

_QUESTION_START = re.compile(
    r"^(what|what's|whats|where|where's|who|who's|which|how|why|is|are|do|does|did)\b",
    re.IGNORECASE,
)

_STATES_TTL_S = 10.0


@dataclass(frozen=True)
class PreFetch:
    tool: str
    args: dict
    rule: str  # entity_id | room | keyword


def entity_id_candidates(text: str) -> set[str]:
    """Every entity_id-shaped token in the message."""
    return set(_ENTITY_ID.findall((text or "").lower()))


def reads_as_question(text: str) -> bool:
    text = (text or "").strip()
    if text.endswith("?"):
        return True
    return bool(_QUESTION_START.match(text.replace("’", "'")))


def _is_compound(text: str) -> bool:
    """A coordination cue, ignoring the question mark that ends a question
    and the dot inside an entity_id."""
    masked = _ENTITY_ID.sub("entity", (text or "").lower())
    return has_coordination_cue(masked.strip().rstrip("?"))


def _room_overview_hit(lowered: str) -> bool:
    for phrase in _ROOM_OVERVIEW:
        if phrase in lowered:
            return True
    return False


def _named_area(text: str, areas: list[dict]) -> str | None:
    """The one area the message names, or None (none, or more than one)."""
    found = []
    for area in areas:
        if names_a_room(text, room_terms([area], [])):
            found.append(area.get("name") or area.get("area_id") or "")
    if len(found) == 1 and found[0]:
        return found[0]
    return None


def _keyword_tools(lowered: str) -> set[str]:
    """Non-core tools whose keywords hit."""
    hits = set()
    for name, keywords in _KEYWORDS.items():
        if name in CORE_TOOLS:
            continue
        for kw in keywords:
            if kw in lowered:
                hits.add(name)
                break
    return hits


def decide_prefetch(text: str, available: set[str] | frozenset[str],
                    areas: list[dict] | None = None,
                    known_entity_ids: set[str] | None = None) -> PreFetch | None:
    """The tool to run before the first model call, or None. Pure: the text,
    the enabled tool names, the area registry and the live entity_ids.
    First hit wins: literal entity_id, room question, zero-argument keyword."""
    if not text or has_deferral_cue(text) or _is_compound(text):
        return None
    lowered = text.lower().replace("’", "'")

    # D1: exactly one entity_id that really exists.
    candidates = entity_id_candidates(text)
    if known_entity_ids is not None:
        real = []
        for eid in candidates:
            if eid in known_entity_ids:
                real.append(eid)
        if len(real) == 1 and "get_entity_state" in available:
            return PreFetch("get_entity_state", {"entity_id": real[0]}, "entity_id")
    if len(candidates) > 1:
        return None

    question = reads_as_question(text)

    # D10: a room question. Gated on a question so "turn on the lights in
    # the kitchen" is not pre-fetched a device list.
    if question and _room_overview_hit(lowered):
        area = _named_area(text, areas or [])
        if area is not None:
            if "list_devices" in available:
                return PreFetch("list_devices", {"area": area}, "room")
            return None
        if "get_areas" in available:
            return PreFetch("get_areas", {}, "room")
        return None

    # D2: one zero-argument tool by keyword. The room tools share their
    # triggers, so they are not candidates here (D10 owns them).
    if not question:
        return None
    hits = _keyword_tools(lowered)
    hits.discard("get_areas")
    hits.discard("list_devices")
    if len(hits) != 1:
        return None
    tool = next(iter(hits))
    if tool in ZERO_ARG_TOOLS and tool in available:
        return PreFetch(tool, {}, "keyword")
    return None


def synthetic_read_call(prefetch: PreFetch) -> AIMessage:
    """A read tool call standing in for the first model response."""
    return AIMessage(
        content="",
        tool_calls=[{"name": prefetch.tool, "args": dict(prefetch.args),
                     "id": PREFETCH_PREFIX + uuid.uuid4().hex}],
        response_metadata={"model_name": PREFETCH_MODEL, "fast_path": True},
        additional_kwargs={"prefetch_rule": prefetch.rule},
    )


class PreFetchMiddleware(AgentMiddleware):
    def __init__(self, rest=None, ws=None, tracer=None, states_ttl_s: float = _STATES_TTL_S):
        super().__init__()
        self._rest = rest
        self._ws = ws
        self._tracer = tracer
        self._ttl_s = states_ttl_s
        self._entity_ids: set[str] | None = None
        self._entity_ids_at = 0.0

    async def _known_entity_ids(self) -> set[str] | None:
        if self._rest is None:
            return None
        now = time.monotonic()
        if self._entity_ids is not None and now - self._entity_ids_at < self._ttl_s:
            return self._entity_ids
        try:
            states = await self._rest.list_states()
        except Exception:
            log.debug("prefetch: state list unavailable")
            return None
        ids = set()
        for state in states:
            entity_id = state.get("entity_id")
            if entity_id:
                ids.add(entity_id)
        self._entity_ids = ids
        self._entity_ids_at = now
        return ids

    async def _areas(self) -> list[dict]:
        # A (re)connecting socket would make the lookup wait out its timeout.
        if not ws_is_ready(self._ws):
            return []
        try:
            return await self._ws.request_cached("config/area_registry/list")
        except Exception:
            log.debug("prefetch: area registry unavailable")
            return []

    async def decide(self, text: str, available: set[str]) -> PreFetch | None:
        """decide_prefetch with the live lookups done only when needed."""
        known = None
        if entity_id_candidates(text):
            known = await self._known_entity_ids()
        areas: list[dict] = []
        if _room_overview_hit(text.lower().replace("’", "'")):
            areas = await self._areas()
        return decide_prefetch(text, available, areas, known)

    def _span(self):
        if self._tracer is None:
            return nullcontext(None)
        try:
            return self._tracer.start_as_current_span(conventions.SPAN_PREFETCH)
        except Exception:
            return nullcontext(None)

    def _record(self, prefetch: PreFetch) -> None:
        try:
            with self._span() as span:
                if span is not None:
                    span.set_attribute(conventions.GOSLING_PREFETCH_TOOL, prefetch.tool)
                    span.set_attribute(conventions.GOSLING_PREFETCH_RULE, prefetch.rule)
        except Exception:
            pass

    async def awrap_model_call(self, request, handler):
        messages = request.messages
        last = messages[-1] if messages else None
        # First step of a run only; the step after the pre-fetch is an
        # ordinary model call with the tool result in context.
        if not isinstance(last, HumanMessage) or opted_out(request):
            return await handler(request)
        available = set()
        for tool in request.tools or []:
            if isinstance(tool, dict):
                available.add(tool.get("name") or "")
            else:
                available.add(getattr(tool, "name", "") or "")
        try:
            prefetch = await self.decide(latest_human_text(messages), available)
        except Exception:
            log.exception("prefetch decision failed; falling through to the model")
            return await handler(request)
        if prefetch is None:
            return await handler(request)
        self._record(prefetch)
        return synthetic_read_call(prefetch)
