"""In-process fan-out of library-change events to connected SSE tabs.

``publish`` is safe to call from ANY thread (request handlers on the event loop
OR a job daemon thread): it hops onto the captured loop via
``call_soon_threadsafe`` before touching the asyncio queues, which are not
themselves thread-safe.
"""

from __future__ import annotations

import asyncio

from app.models.events import LibraryChangedEvent

MAX_QUEUE = 64  # bounded; a stuck tab drops events instead of growing unbounded


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
        self.publish(LibraryChangedEvent().model_dump_json())

    def publish_art_changed(self) -> None:
        self.publish(LibraryChangedEvent(type="art:changed").model_dump_json())

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
