"""open_checkpointer: in-memory by default, durable SQLite when a path is set."""

from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from app.agent.checkpointer import open_checkpointer
from app.agent.memory import BoundedMemorySaver
from app.config import Settings


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _checkpoint() -> dict:
    return {
        "v": 4,
        "id": "c1",
        "ts": "2026-01-01T00:00:00+00:00",
        "channel_values": {"messages": ["hello"]},
        "channel_versions": {},
        "versions_seen": {},
    }


async def test_empty_path_yields_bounded_memory_saver():
    settings = Settings(_env_file=None, checkpoint_db_path="")
    async with open_checkpointer(settings) as saver:
        assert isinstance(saver, BoundedMemorySaver)


async def test_bounded_saver_is_still_the_in_memory_kind():
    # Guards against a future refactor swapping in a durable saver by default.
    settings = Settings(_env_file=None)
    async with open_checkpointer(settings) as saver:
        assert isinstance(saver, MemorySaver)


async def test_path_persists_across_lifetimes(tmp_path: Path):
    db = tmp_path / "checkpoints.sqlite"
    settings = Settings(_env_file=None, checkpoint_db_path=str(db))
    cfg = _config("t1")

    # First "process lifetime": write a checkpoint, then close the saver.
    async with open_checkpointer(settings) as saver:
        assert not isinstance(saver, BoundedMemorySaver)
        await saver.aput(cfg, _checkpoint(), {"source": "input", "step": 0}, {})

    # Second lifetime (fresh saver, same file): the checkpoint survives.
    async with open_checkpointer(settings) as saver2:
        tup = await saver2.aget_tuple(cfg)
        assert tup is not None
        assert tup.checkpoint["channel_values"]["messages"] == ["hello"]

    assert db.exists() and db.stat().st_size > 0
