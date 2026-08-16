"""Single-flight background resolution for uncached artist portraits.

An uncached page used to resolve every portrait INSIDE its HTTP request, under
a shared 5/s limiter with a concurrency cap of 2 — so a 48-artist page spent
~10 s filling while 48 requests sat open. This moves that work off the request
path:

* the first caller for an artist starts ONE task and waits ``grace_seconds`` for
  it. A source that answers quickly (a single artist-detail page) is therefore
  still served inline, with no visible change;
* a caller whose task has not finished in time gets ``None``, answers 404, and
  the browser shows the initials monogram. The task keeps running;
* when a burst of fills drains, ``on_filled`` fires ONCE. The frontend turns
  that ``art:changed`` into a global asset-version bump, which clears
  ``ArtistImage``'s error state and re-requests every portrait — the filled ones
  now 200, the rest answer cheap stat-based 304s.

**The in-flight map is the point.** Nothing else in this app single-flights, so
without it a background filler would multiply the traffic it exists to reduce:
each of a page's N requests for the same artist would start its own resolve, and
each would write the same cache slot. The key is the NORMALIZED artist name —
the same key the cache uses — so twin spellings collapse the way cache entries
do.

**The notification is coalesced for the same reason.** ``ArtistImage`` keys its
``<img>`` on the GLOBAL asset version and the SSE hook bumps it immediately
(un-debounced, by design, so image edits feel instant). One emit per completed
fill would therefore remount all N portraits N times.

This module is deliberately app-free: it takes the service to call and a plain
callback to fire, so it can be unit-tested without a FastAPI app and cannot
reach into ``app.state``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from app.artwork.normalize import normalize_artist_name
from app.artwork.service import ArtistImageService

_log = logging.getLogger("musicdrop.artwork")

#: Quiet gap after the last completed fill before notifying, and the hard cap on
#: how long a continuous stream of fills may defer that notification. Mirrors
#: the frontend's own SSE coalescing constants (FLUSH_AFTER_MS / MAX_WAIT_MS).
_FLUSH_AFTER_SECONDS = 0.5
_MAX_WAIT_SECONDS = 5.0


class ArtistImageFiller:
    """One in-flight resolve per artist, plus one coalesced completion signal."""

    def __init__(
        self,
        *,
        on_filled: Callable[[], None],
        flush_after: float = _FLUSH_AFTER_SECONDS,
        max_wait: float = _MAX_WAIT_SECONDS,
    ) -> None:
        self._on_filled = on_filled
        self._flush_after = flush_after
        self._max_wait = max_wait
        self._inflight: dict[str, asyncio.Task[tuple[bytes, str] | None]] = {}
        self._emit_handle: asyncio.TimerHandle | None = None
        self._emit_deadline = 0.0

    async def fill(
        self,
        service: ArtistImageService,
        name: str,
        *,
        get_mbid: Callable[[], str | None],
        grace_seconds: float,
    ) -> tuple[bytes, str] | None:
        """Resolve ``name``, returning its bytes only if that happens fast.

        Joins the in-flight resolve for this artist when one exists, otherwise
        starts it. Returns the image when the resolve completes inside
        ``grace_seconds`` and ``None`` otherwise — ``None`` means "not yet",
        not "no image": the task continues and ``on_filled`` announces it.

        ``service`` is a parameter rather than state so the object a request's
        dependency injection resolved is the object that does the work.
        """
        key = normalize_artist_name(name)
        task = self._inflight.get(key)
        if task is None:
            # No await between the get and the set: asyncio is single-threaded,
            # so this check-then-act cannot interleave.
            task = asyncio.get_running_loop().create_task(self._run(key, service, name, get_mbid))
            self._inflight[key] = task
        if grace_seconds <= 0:
            return None
        # asyncio.wait, NOT wait_for: wait_for CANCELS on timeout, which would
        # kill the very background fill this exists to start.
        done, _pending = await asyncio.wait({task}, timeout=grace_seconds)
        if not done:
            return None
        return task.result()

    async def _run(
        self,
        key: str,
        service: ArtistImageService,
        name: str,
        get_mbid: Callable[[], str | None],
    ) -> tuple[bytes, str] | None:
        result: tuple[bytes, str] | None = None
        try:
            result = await service.get_artist_image(name, get_mbid=get_mbid)
        except asyncio.CancelledError:
            raise  # shutdown: let cancellation propagate
        except Exception:
            # A background task whose exception is never retrieved logs an
            # unhelpful asyncio warning at GC time and would leave this key
            # wedged in the map, blocking every later attempt for this artist.
            _log.exception("artist-image background fill failed for %r", name)
        finally:
            self._inflight.pop(key, None)
        if result is not None:
            self._arm_notification()
        return result

    def _arm_notification(self) -> None:
        """Debounce the completion signal: fire after a quiet gap, but never
        later than ``max_wait`` after the first fill of this burst."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._emit_handle is None:
            self._emit_deadline = now + self._max_wait
        else:
            self._emit_handle.cancel()
        delay = min(self._flush_after, max(0.0, self._emit_deadline - now))
        self._emit_handle = loop.call_later(delay, self._notify)

    def _notify(self) -> None:
        self._emit_handle = None
        try:
            self._on_filled()
        except Exception:
            # The callback publishes an SSE event; a broker hiccup must not take
            # down the loop callback that fired it.
            _log.exception("artist-image fill notification failed")

    def inflight_count(self) -> int:
        """How many resolves are running. Observability + tests."""
        return len(self._inflight)

    async def close(self) -> None:
        """Cancel outstanding fills and any pending notification (shutdown)."""
        if self._emit_handle is not None:
            self._emit_handle.cancel()
            self._emit_handle = None
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
