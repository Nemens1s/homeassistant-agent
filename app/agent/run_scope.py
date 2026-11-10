"""Per-run facts the harness knows and the agent must not: the user's
language (for notifications), whether notifications are suppressed for this
run, the thread id, and where the request came from (the requesting device's
area and the speaking person, resolved at the HTTP edge).

Callers put them in the LangGraph config ("configurable"); every tool call
receives that config, and the tool adapter exposes it to handlers through
current_scope() for the duration of the call. The agent never sees them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from langgraph.config import get_config

LANGUAGE_KEY = "gosling_language"
SUPPRESS_NOTIFY_KEY = "gosling_suppress_notify"
AREA_ID_KEY = "gosling_area_id"
AREA_NAME_KEY = "gosling_area_name"
PERSON_NAME_KEY = "gosling_person_name"
PERSON_ENTITY_KEY = "gosling_person_entity_id"


@dataclass(frozen=True, slots=True)
class RequestFacts:
    """The requesting device's area and the speaker, when they resolved."""

    area_id: str | None = None
    area_name: str | None = None
    person_name: str | None = None
    person_entity_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return (self.area_id is None and self.area_name is None
                and self.person_name is None and self.person_entity_id is None)


@dataclass(frozen=True, slots=True)
class RunScope:
    language: str = "en"
    suppress_notify: bool = False
    thread_id: str = "default"
    request: RequestFacts = field(default_factory=RequestFacts)


_current: ContextVar[RunScope] = ContextVar("gosling_run_scope", default=RunScope())


def scope_from_config(config) -> RunScope:
    configurable = (config or {}).get("configurable") or {}
    language = configurable.get(LANGUAGE_KEY) or "en"
    suppress = configurable.get(SUPPRESS_NOTIFY_KEY) is True
    thread_id = configurable.get("thread_id") or "default"
    return RunScope(language=language, suppress_notify=suppress, thread_id=thread_id,
                    request=_facts_from_configurable(configurable))


def _facts_from_configurable(configurable: dict) -> RequestFacts:
    # An area or a person is only usable whole: both halves or neither.
    area_id = configurable.get(AREA_ID_KEY)
    area_name = configurable.get(AREA_NAME_KEY)
    if not area_id or not area_name:
        area_id = None
        area_name = None
    person_name = configurable.get(PERSON_NAME_KEY)
    person_entity_id = configurable.get(PERSON_ENTITY_KEY)
    if not person_name or not person_entity_id:
        person_name = None
        person_entity_id = None
    return RequestFacts(area_id, area_name, person_name, person_entity_id)


def scope_from_running_graph() -> RunScope:
    """The scope of the graph run this code executes in (middleware has no
    config argument); the defaults outside a run."""
    try:
        config = get_config()
    except RuntimeError:  # outside a graph run (tests)
        return RunScope()
    return scope_from_config(config)


def scope_configurable(language: str, suppress_notify: bool = False) -> dict:
    """The configurable entries a caller adds next to thread_id."""
    return {LANGUAGE_KEY: language, SUPPRESS_NOTIFY_KEY: suppress_notify}


def facts_configurable(facts: RequestFacts) -> dict:
    """The configurable entries for the request facts; missing ones are left out."""
    entries = {
        AREA_ID_KEY: facts.area_id,
        AREA_NAME_KEY: facts.area_name,
        PERSON_NAME_KEY: facts.person_name,
        PERSON_ENTITY_KEY: facts.person_entity_id,
    }
    result = {}
    for key, value in entries.items():
        if value is not None:
            result[key] = value
    return result


def current_scope() -> RunScope:
    return _current.get()


@contextmanager
def use_scope(scope: RunScope) -> Iterator[RunScope]:
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)
