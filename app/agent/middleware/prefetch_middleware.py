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

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from opentelemetry import trace

from app.agent.middleware.fast_path_middleware import (
    has_coordination_cue,
    has_deferral_cue,
    opted_out,
)
from app.agent.middleware.tool_subset_middleware import latest_human_text
from app.agent.run_scope import scope_from_running_graph
from app.degraded.matcher import is_question
from app.ha.websocket import ws_is_ready
from app.telemetry import conventions
from app.tools.helpers.request_context import named_area

log = logging.getLogger("prefetch")

PREFETCH_PREFIX = conventions.PREFETCH_PREFIX
# The "model" of the synthetic response; telemetry counts the path by it.
PREFETCH_MODEL = conventions.PREFETCH_MODEL

# Domain and object id of at least two characters each, so "a.m" and "u.s"
# are not entity_ids.
_ENTITY_ID = re.compile(r"\b[a-z_]{2,}\.[a-z0-9_]{2,}\b")

# A question starts with a question word. A trailing "?" alone is not
# enough: voice STT adds one to commands ("Start the vacuum?").
_QUESTION_START = re.compile(
    r"^(what|whats|which|who|whos|where|wheres|how|is|are|was|were|do|does|did|"
    r"any|have|has)\b")

# The pre-fetch's own triggers. NOT tool_router's keywords: those decide
# availability and are generous on purpose ("wind" in "window" just adds a
# schema). Here a hit is a choice, so every pattern is word-bounded and
# narrow. Each tool takes no arguments, so a hit is enough to run it.
# Every trigger must also be a tool_router keyword of its tool, or the
# tool is not on the subsetted menu (tests/test_deterministic_evals.py).
_KEYWORD_TRIGGERS: dict[str, re.Pattern] = {
    "get_weather": re.compile(
        r"\b(weather|forecast|raining|rain|snowing|snow|sunny|windy|"
        r"temperature outside|cold outside|warm outside|hot outside)\b"),
    "get_battery_status": re.compile(r"\bbatter(y|ies)\b|\blow on (battery|power)\b"),
    "get_person_locations": re.compile(
        r"\b(anyone|anybody|everyone|everybody|nobody|someone|somebody) "
        r"(is |at )?(home|here|away)\b"
        r"|\bwho('s| is) (at )?(home|here|away)\b"
        r"|\bwhere('s| is) everyone\b"
        r"|^is (?!the\b|it\b|there\b)\w+ (at )?(home|away)\b"),
    "get_vacuum_state": re.compile(r"\b(vacuum|roborock|roomba|robot vacuum)\b"),
    "get_error_log": re.compile(r"\b(errors?|error log|warnings)\b"),
    # Not "reminders": the deferral guard ("remind\w*") stops it first.
    "list_scheduled": re.compile(r"\b(my notes|scheduled|pending (tasks|notes))\b"),
    "list_actions": re.compile(
        r"\bwhat (actions|can you (do|run|trigger))\b|\b(list|which) actions\b"),
}

# A question that needs arguments the pre-fetch does not fill (a time range,
# the logbook, automations): no pre-fetch at all, the model picks. Not
# "today": "what's the weather today?" is a zero-argument read.
_NEEDS_ARGS = re.compile(
    r"\b(history|trend|yesterday|last (hour|night|week|month|\d+)|"
    r"past|since|ago|happened|logbook|graph|chart|automations?|routines?)\b")

# Room questions. Only explicit inventory phrasing: "what's the temperature
# in the kitchen" is a state question, list_devices cannot answer it.
_DEVICES_IN = re.compile(
    r"\bwhat('s| is| do i have) in\b|\b(what|which) devices\b|\bdevices (are )?in\b")
# The whole-home inventory: the only list_devices call without an area.
_ALL_DEVICES = re.compile(r"^(what|which) devices (do i have|are there)\b")
# The room the request came from.
_HERE = re.compile(r"\b(in here|this room)\b")
_ROOM_LIST = re.compile(
    r"\b(what|which) (rooms|areas)\b|\blist (the |my |all )?(rooms|areas)\b"
    r"|\bhow many (rooms|areas)\b|\ball (rooms|areas)\b")

# The synthetic call id is replayed to the model on the phrasing call;
# 24 hex chars keep it at 33, under OpenAI's 40-char tool_call id limit.
_ID_HEX_CHARS = 24

# The existence check runs before the first model call; never let a slow HA
# hold the request (the REST client's own timeout is far longer).
_LOOKUP_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class PreFetch:
    tool: str
    args: dict
    rule: str  # entity_id | room | keyword


@dataclass(frozen=True)
class _Parsed:
    """The message, prepared once for every rule."""
    text: str
    masked: str
    candidates: frozenset[str]
    eligible: bool  # passed the cheap gates

    @property
    def wants_areas(self) -> bool:
        return bool(_DEVICES_IN.search(self.masked))


def _masked(text: str) -> str:
    """Lower-cased, with entity_ids replaced: "sensor.outside_temperature"
    must not read as "outside", nor its dot as a sentence break."""
    lowered = (text or "").lower().replace("’", "'")
    return _ENTITY_ID.sub("entity", lowered).strip()


def _reads_as_question(text: str, masked: str) -> bool:
    """A question, not a polite request ("can you stop the vacuum?" is a
    command; see degraded.matcher.is_question) nor a command with an STT
    question mark."""
    if not is_question(text):
        return False
    return bool(_QUESTION_START.match(masked.replace("'", "")))


def parse(text: str) -> _Parsed:
    masked = _masked(text)
    candidates = frozenset(_ENTITY_ID.findall((text or "").lower()))
    eligible = (
        bool(masked)
        and not has_deferral_cue(masked)
        and not has_coordination_cue(masked.rstrip("?"))
        and _reads_as_question(text or "", masked)
        and not _NEEDS_ARGS.search(masked)
    )
    return _Parsed(text or "", masked, candidates, eligible)


def _keyword_tools(masked: str) -> set[str]:
    hits = set()
    for name, pattern in _KEYWORD_TRIGGERS.items():
        if pattern.search(masked):
            hits.add(name)
    return hits


def _room_prefetch(parsed: _Parsed, hits: set[str], available, areas,
                   request_area: str | None) -> PreFetch | None:
    if hits or "list_devices" not in available:
        return None  # "what's in the vacuum's bin" — not an inventory
    if _HERE.search(parsed.masked):
        if request_area:
            return PreFetch("list_devices", {"area": request_area}, "room")
        return None
    if areas is None:
        return None  # registry unreadable: no guessing
    area = named_area(parsed.text, areas)
    if area is not None:
        name = area.get("name") or area.get("area_id")
        return PreFetch("list_devices", {"area": name}, "room")
    if _ALL_DEVICES.match(parsed.masked):
        return PreFetch("list_devices", {}, "room")
    return None  # "what's in the fridge?" — not a room


def _decide(parsed: _Parsed, available, areas, known_entity_ids,
            request_area: str | None) -> PreFetch | None:
    if not parsed.eligible or len(parsed.candidates) > 1:
        return None
    hits = _keyword_tools(parsed.masked)

    # D1: exactly one entity_id, and it exists.
    if len(parsed.candidates) == 1:
        eid = next(iter(parsed.candidates))
        if known_entity_ids is not None and eid in known_entity_ids:
            if "get_entity_state" in available:
                return PreFetch("get_entity_state", {"entity_id": eid}, "entity_id")
            return None

    # D10: a room question.
    if parsed.wants_areas:
        return _room_prefetch(parsed, hits, available, areas, request_area)
    if _ROOM_LIST.search(parsed.masked):
        if hits or "get_areas" not in available:
            return None
        return PreFetch("get_areas", {}, "room")

    # D2: one zero-argument tool by keyword.
    if len(hits) != 1:
        return None
    tool = next(iter(hits))
    if tool in available:
        return PreFetch(tool, {}, "keyword")
    return None


def decide_prefetch(text: str, available: set[str] | frozenset[str],
                    areas: list[dict] | None = None,
                    known_entity_ids: set[str] | None = None,
                    request_area: str | None = None) -> PreFetch | None:
    """The tool to run before the first model call, or None. Pure: the text,
    the enabled tool names, the area registry (None = could not be read),
    the entity_ids known to exist and the request's area name. First hit
    wins: literal entity_id, room question, zero-argument keyword tool."""
    return _decide(parse(text), available, areas, known_entity_ids, request_area)


def synthetic_read_call(prefetch: PreFetch) -> AIMessage:
    """A read tool call standing in for the first model response."""
    return AIMessage(
        content="",
        tool_calls=[{"name": prefetch.tool, "args": dict(prefetch.args),
                     "id": PREFETCH_PREFIX + uuid.uuid4().hex[:_ID_HEX_CHARS]}],
        response_metadata={"model_name": PREFETCH_MODEL, "fast_path": True},
    )


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


class PreFetchMiddleware(AgentMiddleware):
    def __init__(self, rest=None, ws=None, lookup_timeout_s: float = _LOOKUP_TIMEOUT_S):
        super().__init__()
        self._rest = rest
        self._ws = ws
        self._timeout_s = lookup_timeout_s

    async def _known(self, entity_id: str) -> set[str] | None:
        """{entity_id} when HA has it, an empty set when HA says it does not,
        None when HA could not be asked in time."""
        if self._rest is None:
            return None
        try:
            await asyncio.wait_for(self._rest.get_state(entity_id), self._timeout_s)
        except Exception as exc:
            if _is_not_found(exc):
                return set()
            log.debug("prefetch: state lookup failed for %s", entity_id)
            return None
        return {entity_id}

    async def _areas(self) -> list[dict] | None:
        # A (re)connecting socket would make the lookup wait out its timeout.
        if not ws_is_ready(self._ws):
            return None
        try:
            return await self._ws.request_cached("config/area_registry/list")
        except Exception:
            log.debug("prefetch: area registry unavailable")
            return None

    async def decide(self, text: str, available: set[str],
                     request_area: str | None = None) -> PreFetch | None:
        """The decision, with HA asked only after the cheap gates passed and
        only for what the rule that can still fire needs."""
        parsed = parse(text)
        if not parsed.eligible or len(parsed.candidates) > 1:
            return None
        known = None
        if parsed.candidates:
            known = await self._known(next(iter(parsed.candidates)))
            if known:
                return _decide(parsed, available, None, known, request_area)
        areas = None
        if parsed.wants_areas:
            areas = await self._areas()
        return _decide(parsed, available, areas, known, request_area)

    def _record(self, prefetch: PreFetch) -> None:
        """The rule and tool on the current chat span (TelemetryMiddleware's,
        outer to this one); a no-op without telemetry."""
        try:
            span = trace.get_current_span()
            span.set_attribute(conventions.GOSLING_PREFETCH_TOOL, prefetch.tool)
            span.set_attribute(conventions.GOSLING_PREFETCH_RULE, prefetch.rule)
        except Exception:
            pass

    def wrap_model_call(self, request, handler):
        return handler(request)

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
            request_area = scope_from_running_graph().request.area_name
            prefetch = await self.decide(latest_human_text(messages), available, request_area)
        except Exception:
            log.exception("prefetch decision failed; falling through to the model")
            return await handler(request)
        if prefetch is None:
            return await handler(request)
        self._record(prefetch)
        return synthetic_read_call(prefetch)
