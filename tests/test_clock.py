from datetime import datetime
from types import SimpleNamespace

import pytest

from app.events.clock import (
    FireTimeError,
    current_local_time,
    format_clock,
    from_key,
    parse_clock,
    resolve_fire_at,
    to_key,
)

NOW = datetime(2026, 10, 5, 9, 32)


def test_parse_clock_reads_world_clock_format():
    assert parse_clock("09:32 05-10-2026") == NOW


@pytest.mark.parametrize(
    "raw", [None, "", "unavailable", "unknown", "09:32", "2026-10-05 09:32", "25:00 05-10-2026"]
)
def test_parse_clock_rejects_garbage(raw):
    assert parse_clock(raw) is None


def test_key_round_trip_and_format():
    assert from_key(to_key(NOW)) == NOW
    assert to_key(NOW) == "2026-10-05 09:32"
    assert format_clock(NOW) == "09:32 05-10-2026"


def test_key_order_is_time_order():
    moments = [datetime(2026, 12, 31, 23, 59), datetime(2026, 1, 2, 3, 4), datetime(2027, 1, 1, 0, 0)]
    keys = []
    for moment in moments:
        keys.append(to_key(moment))
    expected = []
    for moment in sorted(moments):
        expected.append(to_key(moment))
    assert sorted(keys) == expected


def test_in_minutes_counts_from_now():
    assert resolve_fire_at(NOW, None, 30) == datetime(2026, 10, 5, 10, 2)


def test_bare_time_later_today():
    assert resolve_fire_at(NOW, "18:00", None) == datetime(2026, 10, 5, 18, 0)


def test_bare_time_already_passed_means_tomorrow():
    assert resolve_fire_at(NOW, "08:00", None) == datetime(2026, 10, 6, 8, 0)


def test_bare_time_equal_to_now_rolls_to_tomorrow():
    assert resolve_fire_at(NOW, "09:32", None) == datetime(2026, 10, 6, 9, 32)


def test_full_future_time():
    assert resolve_fire_at(NOW, "18:00 06-10-2026", None) == datetime(2026, 10, 6, 18, 0)


def test_full_past_time_is_rejected():
    with pytest.raises(FireTimeError) as info:
        resolve_fire_at(NOW, "08:00 05-10-2026", None)
    assert info.value.code == "in_past"


@pytest.mark.parametrize(
    "at, in_minutes",
    [(None, None), ("18:00", 30), ("6pm", None), (None, 0)],
)
def test_bad_combinations_are_invalid(at, in_minutes):
    with pytest.raises(FireTimeError) as info:
        resolve_fire_at(NOW, at, in_minutes)
    assert info.value.code == "invalid_params"


class _ClockRest:
    def __init__(self, state=None, fail=False):
        self._state = state
        self._fail = fail

    async def get_state(self, entity_id):
        if self._fail:
            raise ConnectionError("down")
        return {"entity_id": entity_id, "state": self._state}


def _ctx(rest, clock_entity="sensor.europe_tallinn"):
    return SimpleNamespace(settings=SimpleNamespace(clock_entity=clock_entity), rest=rest)


async def test_current_local_time_reads_the_clock_entity():
    assert await current_local_time(_ctx(_ClockRest("09:32 05-10-2026"))) == NOW


async def test_current_local_time_none_when_off_or_unreadable():
    assert await current_local_time(_ctx(_ClockRest("09:32 05-10-2026"), clock_entity="")) is None
    assert await current_local_time(_ctx(_ClockRest(fail=True))) is None
    assert await current_local_time(_ctx(_ClockRest("unavailable"))) is None
