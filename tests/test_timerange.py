"""Relative time-range resolution: keeps timestamp arithmetic out of the model.
Small models are unreliable at constructing ISO8601 strings, so history/logbook
tools take a `range` keyword and the handler resolves it here against a fixed now.
"""

from datetime import datetime

from app.tools.helpers.timerange import RANGE_VALUES, resolve_range

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
