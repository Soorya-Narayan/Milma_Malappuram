"""In-process pub/sub for live poll snapshots.

Pollers are the sole publisher; WebSocket connections and any
diagnostic tasks are subscribers. Stays entirely in one asyncio event
loop — no Redis, no message broker. Use case is single-process, single
worker (see CLAUDE.md § "Things to NOT do").

Slow-subscriber policy: if a subscriber's queue grows past
``MAX_QUEUE_DEPTH``, we drop the oldest messages so the poller is
never blocked on a stalled WebSocket. A warning is logged with the
subscriber id so operators can see which client is falling behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

MAX_QUEUE_DEPTH = 50


class Broadcaster:
    """Fan-out pub/sub with bounded per-subscriber queues."""

    def __init__(self) -> None:
        self._subscribers: dict[int, asyncio.Queue[Any]] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Any]]:
        """Register a subscriber queue; unregister on context exit."""
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=MAX_QUEUE_DEPTH)
        sub_id = next(self._ids)
        async with self._lock:
            self._subscribers[sub_id] = q
        log.debug("subscriber %d attached (total=%d)", sub_id, len(self._subscribers))
        try:
            yield q
        finally:
            async with self._lock:
                self._subscribers.pop(sub_id, None)
            log.debug("subscriber %d detached (total=%d)", sub_id, len(self._subscribers))

    def publish(self, payload: Any) -> None:
        """Non-blocking fan-out to every subscriber.

        If a queue is full, drop the oldest message and push the new one.
        This preserves "latest state" semantics at the cost of occasional
        gaps for clients that can't keep up.
        """
        for sub_id, q in list(self._subscribers.items()):
            while True:
                try:
                    q.put_nowait(payload)
                    break
                except asyncio.QueueFull:
                    try:
                        _ = q.get_nowait()  # drop oldest
                    except asyncio.QueueEmpty:  # pragma: no cover - race
                        break
                    log.warning(
                        "subscriber %d slow, dropped a snapshot (queue=%d)",
                        sub_id,
                        q.qsize(),
                    )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
