"""In-process fan-out of library-change events to connected SSE tabs.

``publish`` is safe to call from ANY thread (request handlers on the event loop
OR a job daemon thread): it hops onto the captured loop via
``call_soon_threadsafe`` before touching the asyncio queues, which are not
themselves thread-safe.
"""

from __future__ import annotations

import asyncio

from app.beets.browse import invalidate_browse_cache
from app.models.events import LibraryChangedEvent

MAX_QUEUE = 64  # bounded; a stuck tab drops events instead of growing unbounded
MAX_SUBSCRIBERS = 32  # cap concurrent SSE streams so a client can't exhaust memory


class EventBroker:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._subscribers: set[asyncio.Queue[str]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def publish_library_changed(self) -> None:
        # Invalidate here, not only in emit.py's helper: the import registry
        # holds the broker directly and publishes without going through emit.
        # (emit.py keeps its own call for the broker-less/lifespan-less paths.)
        invalidate_browse_cache()
        self.publish(LibraryChangedEvent().model_dump_json(exclude_none=True))

    def publish_art_changed(self, scope: str | None = None) -> None:
        """``scope`` = which asset's bytes changed (``"album:12"`` /
        ``"artist:ABBA"``); ``None`` means library-wide.

        ``exclude_none`` keeps an unscoped event's bytes exactly as before this
        field existed (``{"type": ...}``), so the wire contract only grows when
        there IS a scope; the frontend reads a missing scope as "bump globally".
        """
        self.publish(
            LibraryChangedEvent(type="art:changed", scope=scope).model_dump_json(exclude_none=True)
        )

    def publish(self, event: str) -> None:
        try:
            self._loop.call_soon_threadsafe(self._fanout, event)
        except RuntimeError:
            pass  # loop closed/stopped at shutdown — dropping the event is correct

    def _fanout(self, event: str) -> None:
        for q in list(self._subscribers):  # snapshot: safe against mid-iteration change
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop: every event is an idempotent refetch trigger
