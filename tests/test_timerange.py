"""Relative time-range resolution: keeps timestamp arithmetic out of the model.
Small models are unreliable at constructing ISO8601 strings, so history/logbook
tools take a `range` keyword and the handler resolves it here against a fixed now.
"""

from datetime import datetime, timedelta, timezone

from app.tools.helpers.timerange import RANGE_VALUES, resolve_range, to_local_iso

NOW = datetime.fromisoformat("2026-08-03T14:30:00+02:00")


def test_last_hour_ends_at_now():
    start, end = resolve_range("last_hour", now=NOW)
    assert start == "2026-08-03T13:30:00+02:00"
    assert end is None  # None means "up to now"


def test_last_24h():
    start, end = resolve_range("last_24h", now=NOW)
    assert start == "2026-08-02T14:30:00+02:00"
    assert end is None


def test_today_starts_at_local_midnight():
    start, end = resolve_range("today", now=NOW)
    assert start == "2026-08-03T00:00:00+02:00"
    assert end is None


def test_yesterday_is_a_closed_day_window():
    start, end = resolve_range("yesterday", now=NOW)
    assert start == "2026-08-02T00:00:00+02:00"
    assert end == "2026-08-03T00:00:00+02:00"


def test_last_7d():
    start, end = resolve_range("last_7d", now=NOW)
    assert start == "2026-07-27T14:30:00+02:00"
    assert end is None


def test_last_30d():
    start, end = resolve_range("last_30d", now=NOW)
    assert start == "2026-07-04T14:30:00+02:00"
    assert end is None


def test_unknown_range_raises():
    import pytest

    with pytest.raises(ValueError):
        resolve_range("last_century", now=NOW)


def test_range_values_matches_resolver():
    # Every advertised value must resolve without error.
    for value in RANGE_VALUES:
        resolve_range(value, now=NOW)


# HA returns logbook/history timestamps in UTC, but the agent's injected current
# time is local — to_local_iso converts rows so the small model never has to do
# timezone arithmetic to compare them.
def test_to_local_iso_converts_utc_to_local_zone():
    tz = timezone(timedelta(hours=3))
    assert to_local_iso("2026-09-21T17:27:00+00:00", tz=tz) == "2026-09-21T20:27:00+03:00"


def test_to_local_iso_drops_microseconds():
    tz = timezone(timedelta(hours=3))
    assert to_local_iso("2026-09-21T17:11:57.023910+00:00", tz=tz) == "2026-09-21T20:11:57+03:00"


def test_to_local_iso_assumes_utc_when_naive():
    tz = timezone(timedelta(hours=2))
    assert to_local_iso("2026-09-21T17:00:00", tz=tz) == "2026-09-21T19:00:00+02:00"


def test_to_local_iso_passes_through_empty():
    assert to_local_iso("", tz=timezone.utc) == ""


def test_to_local_iso_passes_through_unparseable():
    assert to_local_iso("not a timestamp", tz=timezone.utc) == "not a timestamp"
