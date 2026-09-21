"""Relative time-range keywords → concrete ISO8601 timestamps.

Timestamp arithmetic ("what is yesterday in ISO8601?") is exactly the kind of
task tiny models fail at. History/logbook tools therefore expose a small enum of
relative windows and resolve it here, so the model never constructs a timestamp.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Literal, get_args

# Advertised to the LLM as a Literal (JSON-schema enum). Keep this list short:
# every extra value is a choice a small model can get wrong.
TimeRange = Literal["last_hour", "last_24h", "today", "yesterday", "last_7d", "last_30d"]

RANGE_VALUES: tuple[str, ...] = get_args(TimeRange)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def to_local_iso(value: str, tz: tzinfo | None = None) -> str:
    """Convert an ISO8601 timestamp to local wall-clock time.

    HA's logbook/history endpoints return timestamps in UTC (a trailing +00:00),
    but the agent's injected "current time" is local. Feeding the model rows in a
    different zone forces it to do timezone arithmetic just to decide whether a row
    is within the window it asked for — exactly the arithmetic small models fumble.
    Converting rows to local keeps both sides in the same zone.

    tz defaults to the system local zone. A naive timestamp is assumed to be UTC.
    Empty or unparseable values are passed through unchanged (never crash a handler).
    """
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).isoformat(timespec="seconds")


def resolve_range(value: str, now: datetime | None = None) -> tuple[str, str | None]:
    """Map a relative range keyword to (start_iso, end_iso).

    end_iso is None for open-ended "up to now" windows; the caller passes None
    straight through to the HA API (which defaults the end to now).
    """
    now = now or datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "last_hour":
        return _iso(now - timedelta(hours=1)), None
    if value == "last_24h":
        return _iso(now - timedelta(hours=24)), None
    if value == "today":
        return _iso(midnight), None
    if value == "yesterday":
        return _iso(midnight - timedelta(days=1)), _iso(midnight)
    if value == "last_7d":
        return _iso(now - timedelta(days=7)), None
    if value == "last_30d":
        return _iso(now - timedelta(days=30)), None
    raise ValueError(f"unknown range: {value!r}")
