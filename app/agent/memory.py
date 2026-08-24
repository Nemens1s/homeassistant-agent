"""Bounded in-memory checkpointer.

MemorySaver keeps every checkpoint for every thread_id forever. For a
long-lived addon this is a slow leak: each turn appends to the stored state,
and threads accumulate across restarts until the process exits.

BoundedMemorySaver adds LRU thread eviction: once `max_threads` distinct
thread IDs have been written, the least-recently-used thread is purged
(storage + blobs + pending writes) before the new thread is admitted.

Note — per-thread message growth: each checkpoint stores the full accumulated
message list, so a single very long conversation still grows within its
thread. ContextWindowMiddleware already caps what the LLM *sees* per call;
the checkpoint just keeps the full history as the source of truth. This is
acceptable for the typical short HA interaction, but a future improvement
could cap stored checkpoints per thread (last N turns only).
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from langgraph.checkpoint.memory import MemorySaver

log = logging.getLogger("agent.memory")

_DEFAULT_MAX_THREADS = 50


class BoundedMemorySaver(MemorySaver):
    """MemorySaver with LRU eviction of old threads."""

    def __init__(self, max_threads: int = _DEFAULT_MAX_THREADS, **kwargs):
        super().__init__(**kwargs)
        self._max_threads = max_threads
        self._thread_lru: OrderedDict[str, None] = OrderedDict()

    def put(self, config, checkpoint, metadata, new_versions):
        thread_id = config["configurable"]["thread_id"]

        if thread_id not in self._thread_lru and len(self._thread_lru) >= self._max_threads:
            evicted_id, _ = self._thread_lru.popitem(last=False)
            self._evict(evicted_id)

        self._thread_lru.pop(thread_id, None)
        self._thread_lru[thread_id] = None

        return super().put(config, checkpoint, metadata, new_versions)

    def _evict(self, thread_id: str) -> None:
        self.storage.pop(thread_id, None)
        for k in [k for k in self.blobs if k[0] == thread_id]:
            del self.blobs[k]
        for k in [k for k in self.writes if k[0] == thread_id]:
            del self.writes[k]
        log.debug("evicted checkpoint memory for thread %s", thread_id)
