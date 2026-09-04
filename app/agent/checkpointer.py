"""Checkpointer selection.

The checkpointer holds conversation state per thread_id. Two options:

- BoundedMemorySaver (default): in-memory, LRU-evicts old threads. Fast, but
  every conversation is lost on restart.
- AsyncSqliteSaver: durable SQLite, so threads survive addon restarts. Enabled
  by setting checkpoint_db_path (in the addon, point it at /data to persist).

Both are handed out through an async context manager so the SQLite connection is
opened before the agent is built and closed on teardown. The langgraph sqlite
dependency is imported lazily — deployments that stay in-memory never need it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from app.agent.memory import BoundedMemorySaver
from app.config import Settings

log = logging.getLogger("agent.memory")


@asynccontextmanager
async def open_checkpointer(settings: Settings):
    path = settings.checkpoint_db_path
    if not path:
        yield BoundedMemorySaver()
        return

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # optional dep

    log.info("using durable checkpointer at %s", path)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        yield saver
