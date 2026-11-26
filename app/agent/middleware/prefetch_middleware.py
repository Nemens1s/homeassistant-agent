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
from app.degraded.matcher import is_question
from app.ha.websocket import ws_is_ready
from app.telemetry import conventions
from app.tools.helpers.request_context import names_a_room, room_terms

log = logging.getLogger("prefetch")

PREFETCH_PREFIX = "prefetch-"
# The "model" of the synthetic response; telemetry counts the path by it.
PREFETCH_MODEL = conventions.PREFETCH_MODEL

_ENTITY_ID = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")

# The pre-fetch's own triggers. NOT tool_router's keywords: those decide
# availability and are generous on purpose ("wind" in "window" just adds a
# schema). Here a hit is a choice, so every pattern is word-bounded and
# narrow. Each tool takes no arguments, so a hit is enough to run it.
_KEYWORD_TRIGGERS: dict[str, re.Pattern] = {
    "get_weather": re.compile(
        r"\b(weather|forecast|raining|rainy|rain|snowing|snow|sunny|windy|"
        r"temperature outside|cold outside|warm outside|hot outside)\b"),
    "get_battery_status": re.compile(r"\bbatter(y|ies)\b|\blow on (battery|power)\b"),
    "get_person_locations": re.compile(
        r"\b(anyone|anybody|everyone|everybody|nobody|someone|somebody) "
        r"(is |at )?(home|here|in|out|away)\b"
        r"|\bwho('s| is) (at )?(home|here|in|out|away)\b"
        r"|\bwhere('s| is) everyone\b"
        r"|^is (?!the\b|it\b|there\b)\w+ (at )?(home|away|out)\b"),
    "get_vacuum_state": re.compile(r"\b(vacuum|roborock|roomba|robot vacuum)\b"),
    "get_error_log": re.compile(r"\b(errors?|error log|warnings)\b"),
    # Not "reminders": the deferral guard ("remind\w*") stops it first.
    "list_scheduled": re.compile(r"\b(my notes|scheduled|pending (tasks|notes))\b"),
    "list_actions": re.compile(
        r"\bwhat (actions|can you (do|run|trigger))\b|\b(list|which) actions\b"),
}

# A question that needs arguments the pre-fetch does not fill (a time range,
# the logbook, automations): no pre-fetch at all, the model picks.
_NEEDS_ARGS = re.compile(
    r"\b(history|trend|yesterday|today|tonight|last (hour|night|week|month|\d+)|"
    r"past|since|ago|happened|logbook|graph|chart|automations?|routines?)\b")

# Room questions. Only explicit inventory phrasing: "what's the temperature
# in the kitchen" is a state question, list_devices cannot answer it.
_DEVICES_IN = re.compile(
    r"\bwhat('s| is| do i have) in\b|\b(what|which) devices\b|\bdevices (are )?in\b")
_ROOM_LIST = re.compile(
    r"\b(what|which) (rooms|areas)\b|\blist (the |my |all )?(rooms|areas)\b"
    r"|\bhow many (rooms|areas)\b|\ball (rooms|areas)\b")

# The synthetic call id is replayed to the model on the phrasing call;
# 24 hex chars keep it at 33, under OpenAI's 40-char tool_call id limit.
_ID_HEX_CHARS = 24


@dataclass(frozen=True)
class PreFetch:
    tool: str
    args: dict
    rule: str  # entity_id | room | keyword


def entity_id_candidates(text: str) -> set[str]:
    """Every entity_id-shaped token in the message."""
    return set(_ENTITY_ID.findall((text or "").lower()))


def _masked(text: str) -> str:
    """Lower-cased, with entity_ids replaced: "sensor.outside_temperature"
    must not read as "outside", nor its dot as a sentence break."""
    lowered = (text or "").lower().replace("\u2019", "'")
    return _ENTITY_ID.sub("entity", lowered).strip()


def reads_as_question(text: str) -> bool:
    """A question, not a polite request: "can you stop the vacuum?" is a
    command (voice STT adds the "?"); see degraded.matcher.is_question."""
    text = (text or "").strip()
    if is_question(text):
        return True
    return bool(re.match(r"(where|who)\b", text.lower()))


def passes_gates(text: str) -> bool:
    """The cheap checks every rule needs: a single, present-tense question
    that needs no arguments the pre-fetch cannot fill."""
    if not text or has_deferral_cue(text):
        return False
    masked = _masked(text)
    if has_coordination_cue(masked.rstrip("?")):
        return False
    if not reads_as_question(text):
        return False
    return not _NEEDS_ARGS.search(masked)


def _keyword_tools(masked: str) -> set[str]:
    hits = set()
    for name, pattern in _KEYWORD_TRIGGERS.items():
        if pattern.search(masked):
            hits.add(name)
    return hits


def _named_areas(text: str, areas: list[dict]) -> list[str]:
    found = []
    for area in areas:
        if names_a_room(text, room_terms([area], [])):
            found.append(area.get("name") or area.get("area_id") or "")
    return found


def needs_areas(text: str) -> bool:
    return bool(_DEVICES_IN.search(_masked(text)))


def decide_prefetch(text: str, available: set[str] | frozenset[str],
                    areas: list[dict] | None = None,
                    known_entity_ids: set[str] | None = None) -> PreFetch | None:
    """The tool to run before the first model call, or None. Pure: the text,
    the enabled tool names, the area registry (None = could not be read) and
    the entity_ids known to exist. First hit wins: literal entity_id, room
    question, zero-argument keyword tool."""
    if not passes_gates(text):
        return None
    masked = _masked(text)
    hits = _keyword_tools(masked)

    # D1: exactly one entity_id, and it exists.
    candidates = entity_id_candidates(text)
    if len(candidates) > 1:
        return None
    if len(candidates) == 1:
        eid = next(iter(candidates))
        if known_entity_ids is not None and eid in known_entity_ids:
            if "get_entity_state" in available:
                return PreFetch("get_entity_state", {"entity_id": eid}, "entity_id")
            return None

    # D10: a room question.
    if _DEVICES_IN.search(masked):
        if areas is None:
            return None  # registry unreadable: no guessing
        named = _named_areas(text, areas)
        if len(named) > 1 or (len(named) == 1 and not named[0]):
            return None  # ambiguous ("bedroom" vs "master bedroom")
        if hits:
            return None  # "what's in the vacuum's bin" — not an inventory
        if "list_devices" not in available:
            return None
        if named:
            return PreFetch("list_devices", {"area": named[0]}, "room")
        return PreFetch("list_devices", {}, "room")
    if _ROOM_LIST.search(masked):
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


def synthetic_read_call(prefetch: PreFetch) -> AIMessage:
    """A read tool call standing in for the first model response."""
    return AIMessage(
        content="",
        tool_calls=[{"name": prefetch.tool, "args": dict(prefetch.args),
                     "id": PREFETCH_PREFIX + uuid.uuid4().hex[:_ID_HEX_CHARS]}],
        response_metadata={"model_name": PREFETCH_MODEL, "fast_path": True},
        additional_kwargs={"prefetch_rule": prefetch.rule},
    )


class PreFetchMiddleware(AgentMiddleware):
    def __init__(self, rest=None, ws=None, tracer=None):
        super().__init__()
        self._rest = rest
        self._ws = ws
        self._tracer = tracer

    async def _known(self, entity_id: str) -> set[str] | None:
        """{entity_id} when HA has it, an empty set when it does not, None
        when HA could not be asked. One point read, not the state dump."""
        if self._rest is None:
            return None
        try:
            await self._rest.get_state(entity_id)
        except Exception:
            return set()
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

    async def decide(self, text: str, available: set[str]) -> PreFetch | None:
        """decide_prefetch, with the HA lookups done only after the cheap
        gates passed and only when a rule needs them."""
        if not passes_gates(text):
            return None
        known = None
        candidates = entity_id_candidates(text)
        if len(candidates) > 1:
            return None
        if len(candidates) == 1:
            known = await self._known(next(iter(candidates)))
        areas = None
        if needs_areas(text):
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
