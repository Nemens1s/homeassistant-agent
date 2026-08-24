from unittest.mock import MagicMock

from app.agent.memory import BoundedMemorySaver


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": "c1"}}


def _put(saver, thread_id: str) -> None:
    config = _config(thread_id)
    checkpoint = {"id": "c1", "v": 1, "ts": "2026-01-01", "channel_versions": {},
                  "versions_seen": {}, "pending_sends": [], "channel_values": {}}
    saver.put(config, checkpoint, {}, {})


def test_within_cap_keeps_all_threads():
    saver = BoundedMemorySaver(max_threads=3)
    for t in ["a", "b", "c"]:
        _put(saver, t)
    assert set(saver.storage.keys()) == {"a", "b", "c"}


def test_over_cap_evicts_lru_thread():
    saver = BoundedMemorySaver(max_threads=2)
    _put(saver, "a")
    _put(saver, "b")
    _put(saver, "c")  # "a" should be evicted (least recently used)
    assert "a" not in saver.storage
    assert "b" in saver.storage
    assert "c" in saver.storage


def test_reuse_refreshes_lru_order():
    saver = BoundedMemorySaver(max_threads=2)
    _put(saver, "a")
    _put(saver, "b")
    _put(saver, "a")  # refresh "a" — now "b" is the LRU
    _put(saver, "c")  # "b" should be evicted, not "a"
    assert "b" not in saver.storage
    assert "a" in saver.storage
    assert "c" in saver.storage


def test_eviction_clears_blobs_and_writes():
    saver = BoundedMemorySaver(max_threads=1)
    _put(saver, "a")
    # Manually plant a blob and write entry for "a"
    saver.blobs[("a", "", "messages", 1)] = ("json", b"[]")
    saver.writes[("a", "", "c1")] = {(0, 0): ("t1", "w", ("json", b""), "")}

    _put(saver, "b")  # triggers eviction of "a"

    assert "a" not in saver.storage
    assert not any(k[0] == "a" for k in saver.blobs)
    assert not any(k[0] == "a" for k in saver.writes)
