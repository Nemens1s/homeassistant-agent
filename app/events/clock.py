"""The HA clock entity is the only source of "now" for time notes.

sensor.europe_tallinn (World Clock integration) reports local wall time as
"HH:MM DD-MM-YYYY" and updates every minute. Time notes are stored and
compared in that same wall time, so the add-on container's timezone never
matters. During the DST fall-back hour a wall time occurs twice; a note fires
on the first one.
"""

from __future__ import annotations

from datetime import datetime, timedelta

CLOCK_FORMAT = "%H:%M %d-%m-%Y"
KEY_FORMAT = "%Y-%m-%d %H:%M"  # stored in the DB: string order == time order
_BARE_TIME_FORMAT = "%H:%M"


class FireTimeError(ValueError):
    """A time note's requested time cannot be scheduled. `code` is the
    ToolResult error code the tool returns."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_clock(state: str | None) -> datetime | None:
    if not state:
        return None
    try:
        return datetime.strptime(state.strip(), CLOCK_FORMAT)
    except ValueError:
        return None


def to_key(moment: datetime) -> str:
    return moment.strftime(KEY_FORMAT)


def from_key(key: str) -> datetime:
    return datetime.strptime(key, KEY_FORMAT)


def format_clock(moment: datetime) -> str:
    return moment.strftime(CLOCK_FORMAT)


async def current_local_time(ctx) -> datetime | None:
    """Point-read the clock entity. None when time notes are off or the clock
    cannot be read; the caller turns that into a clock_unavailable envelope."""
    entity_id = ctx.settings.clock_entity
    if not entity_id:
        return None
    try:
        state = await ctx.rest.get_state(entity_id)
    except Exception:
        return None
    return parse_clock(state.get("state"))


def resolve_fire_at(now: datetime, at: str | None, in_minutes: int | None) -> datetime:
    """When a time note fires. `at` is "HH:MM" (the next time that clock time
    comes round — tomorrow if it is now or already past) or "HH:MM DD-MM-YYYY";
    `in_minutes` counts from now. Exactly one must be given."""
    if (at is None) == (in_minutes is None):
        raise FireTimeError("invalid_params", "Give exactly one of 'at' or 'in_minutes'.")

    if in_minutes is not None:
        if in_minutes < 1:
            raise FireTimeError("invalid_params", "'in_minutes' must be at least 1.")
        return now + timedelta(minutes=in_minutes)

    text = at.strip()
    full = parse_clock(text)
    if full is not None:
        if full <= now:
            raise FireTimeError(
                "in_past", f"{text} has already passed (it is now {format_clock(now)})."
            )
        return full

    try:
        clock_time = datetime.strptime(text, _BARE_TIME_FORMAT).time()
    except ValueError:
        raise FireTimeError(
            "invalid_params", "'at' must look like 'HH:MM' or 'HH:MM DD-MM-YYYY'."
        ) from None
    candidate = datetime.combine(now.date(), clock_time)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate
