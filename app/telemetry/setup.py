"""OTel TracerProvider lifecycle for Agent Gosling.

Usage:
    provider = init_telemetry(settings)   # None when disabled or DB unwritable
    tracer   = get_tracer()               # no-op when not initialised
    # ... at shutdown:
    shutdown_telemetry(provider)          # flush & close
"""

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.telemetry.sqlite_exporter import SqliteSpanExporter
from app.telemetry.store import open_store

_log = logging.getLogger("telemetry")


def init_telemetry(settings) -> TracerProvider | None:
    """Initialise the global TracerProvider.

    Returns None (fail open) when:
    - telemetry_enabled is False, or
    - telemetry_db_path is empty, or
    - opening the SQLite store raises any Exception (logs once).

    On success, registers the provider globally via trace.set_tracer_provider
    and returns it for later shutdown.
    """
    if not settings.telemetry_enabled:
        return None

    if not settings.telemetry_db_path:
        return None

    try:
        conn = open_store(settings.telemetry_db_path)
    except Exception as exc:
        _log.warning("telemetry disabled: could not open store at %r: %s", settings.telemetry_db_path, exc)
        return None

    resource = Resource.create({"service.name": "gosling"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(SqliteSpanExporter(conn)))
    trace.set_tracer_provider(provider)
    return provider


def shutdown_telemetry(provider: TracerProvider | None) -> None:
    """Flush pending spans and shut down the provider.

    Safe to call with None (no-op) or when shutdown raises (bounded).
    """
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception as exc:
        _log.warning("telemetry shutdown error (ignored): %s", exc)


def get_tracer(name: str = "gosling"):
    """Return a tracer from the global provider.

    Returns a no-op tracer when init_telemetry has not been called.
    """
    return trace.get_tracer(name)
