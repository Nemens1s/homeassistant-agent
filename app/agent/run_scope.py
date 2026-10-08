"""Per-run facts the harness knows and the agent must not: the user's
language (for notifications), whether notifications are suppressed for this
run, and the thread id.

Callers put them in the LangGraph config ("configurable"); every tool call
receives that config, and the tool adapter exposes it to handlers through
current_scope() for the duration of the call. The agent never sees them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

LANGUAGE_KEY = "gosling_language"
SUPPRESS_NOTIFY_KEY = "gosling_suppress_notify"


@dataclass(frozen=True, slots=True)
class RunScope:
    language: str = "en"
    suppress_notify: bool = False
    thread_id: str = "default"


_current: ContextVar[RunScope] = ContextVar("gosling_run_scope", default=RunScope())


def scope_from_config(config) -> RunScope:
    configurable = (config or {}).get("configurable") or {}
    language = configurable.get(LANGUAGE_KEY) or "en"
    suppress = configurable.get(SUPPRESS_NOTIFY_KEY) is True
    thread_id = configurable.get("thread_id") or "default"
    return RunScope(language=language, suppress_notify=suppress, thread_id=thread_id)


def scope_configurable(language: str, suppress_notify: bool = False) -> dict:
    """The configurable entries a caller adds next to thread_id."""
    return {LANGUAGE_KEY: language, SUPPRESS_NOTIFY_KEY: suppress_notify}


def current_scope() -> RunScope:
    return _current.get()


@contextmanager
def use_scope(scope: RunScope) -> Iterator[RunScope]:
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)
