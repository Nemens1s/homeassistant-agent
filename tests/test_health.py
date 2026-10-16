import asyncio

from app.health import DOWN, UNKNOWN, UP, ServiceHealth


def _probe(results):
    calls = []

    async def probe():
        calls.append(1)
        value = results.pop(0) if results else True
        if isinstance(value, Exception):
            raise value
        return value

    return probe, calls


async def test_first_ok_probe_is_up_without_recovery_callback():
    probe, _ = _probe([True])
    health = ServiceHealth("llm", probe)
    fired = []

    async def callback():
        fired.append(1)

    health.on_recovery(callback)
    assert health.state == UNKNOWN
    await health.check()
    assert health.state == UP
    assert fired == []


async def test_failed_probe_marks_down():
    probe, _ = _probe([False])
    health = ServiceHealth("llm", probe)
    await health.check()
    assert health.state == DOWN
    assert health.is_down
    assert health.last_error == "probe failed"


async def test_probe_exception_marks_down_with_its_message():
    probe, _ = _probe([ConnectionError("refused")])
    health = ServiceHealth("llm", probe)
    await health.check()
    assert health.state == DOWN
    assert "refused" in health.last_error


async def test_recovery_callback_fires_once_on_down_to_up():
    probe, _ = _probe([False, True, True])
    health = ServiceHealth("llm", probe)
    fired = []

    async def callback():
        fired.append(1)

    health.on_recovery(callback)
    await health.check()
    await health.check()
    await health.check()
    assert fired == [1]
    assert health.last_error is None


async def test_failing_callback_does_not_break_recovery():
    probe, _ = _probe([])
    health = ServiceHealth("llm", probe)

    async def callback():
        raise RuntimeError("boom")

    health.on_recovery(callback)
    health.mark_down("x")
    await health.mark_up()
    assert health.state == UP


def test_mark_down_keeps_original_since_but_updates_error():
    probe, _ = _probe([])
    health = ServiceHealth("llm", probe, initial=UP)
    health.mark_down("first")
    since = health.since
    health.mark_down("second")
    assert health.since == since
    assert health.last_error == "second"


def test_interval_depends_on_state():
    probe, _ = _probe([])
    health = ServiceHealth("llm", probe, down_interval_s=15, up_interval_s=60)
    assert health.interval() == 15
    health.state = UP
    assert health.interval() == 60
    health.mark_down()
    assert health.interval() == 15


async def test_start_probes_in_background_and_stop_cancels():
    probe, calls = _probe([True])
    health = ServiceHealth("llm", probe, up_interval_s=3600)
    health.start()
    await asyncio.sleep(0.01)
    assert calls == [1]
    assert health.state == UP
    await health.stop()
    assert health._task is None


def test_snapshot_shape():
    probe, _ = _probe([])
    health = ServiceHealth("llm", probe, initial=DOWN)
    health.mark_down("boom")
    snap = health.snapshot()
    assert snap["state"] == "down"
    assert snap["error"] == "boom"
    assert "T" in snap["since"]
    assert len(health.since_hhmm()) == 5


async def test_passive_mark_down_wakes_the_loop_for_an_immediate_probe():
    probe, calls = _probe([True, True])
    health = ServiceHealth("llm", probe, up_interval_s=3600)
    health.start()
    await asyncio.sleep(0.01)
    assert health.state == UP
    assert len(calls) == 1
    health.mark_down("request failed")
    await asyncio.sleep(0.1)
    assert len(calls) == 2
    assert health.state == UP
    await health.stop()


async def test_failed_probe_does_not_wake_the_loop():
    probe, calls = _probe([False, False, False])
    health = ServiceHealth("llm", probe, down_interval_s=3600)
    health.start()
    await asyncio.sleep(0.05)
    assert health.state == DOWN
    assert len(calls) == 1
    await health.stop()


async def test_set_probe_replaces_the_probe():
    first, first_calls = _probe([False])
    second, second_calls = _probe([True])
    health = ServiceHealth("llm", first)
    health.set_probe(second)
    await health.check()
    assert first_calls == []
    assert second_calls == [1]
    assert health.state == UP
