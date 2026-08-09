"""Artist-image orchestrator — the single entry point for callers.

``get_artist_image(name)`` resolution order:

1. **disabled?** -> ``None`` (no I/O at all).
2. **cache**: override -> serve; positive -> serve; unexpired negative -> ``None``.
3. **resolve** via the source, under the rate limiter (paces the first-load
   burst):
   * success -> store positive + serve;
   * confirmed no-match (``None``) -> store negative with the LONG TTL + ``None``;
   * :class:`TransientSourceError` -> store negative with the SHORT TTL + ``None``.

Splitting the TTL means a Deezer blip (429/timeout/malformed body/bad download)
only benches an artist for minutes, while a genuine no-match is honored for days.
Each artist costs at most one network resolution until its marker expires.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi.concurrency import run_in_threadpool

from app.artwork.cache import NEGATIVE, ArtistImageCache, CachedImage
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.source import ArtistImageSource, TransientSourceError


class ArtistImageService:
    def __init__(
        self,
        *,
        source: ArtistImageSource,
        cache: ArtistImageCache,
        limiter: TokenBucketLimiter,
        is_enabled: Callable[[], bool],
        negative_ttl_seconds: float,
        transient_ttl_seconds: float,
    ) -> None:
        self._source = source
        self._cache = cache
        self._limiter = limiter
        self._is_enabled = is_enabled
        self._negative_ttl_seconds = negative_ttl_seconds
        self._transient_ttl_seconds = transient_ttl_seconds

    def is_enabled(self) -> bool:
        """Whether artist-image fetching is on right now (the image toggle OR
        the write-to-library toggle -- see main.py's composition).

        Read live, never cached: the predicate is injected and both toggles flip
        at runtime.
        """
        return self._is_enabled()

    def limiter_slot(self) -> AbstractAsyncContextManager[None]:
        """This service's OWN outbound rate/concurrency slot, for callers that
        resolve a source directly (the manual per-source fetch).

        Handing back ``self._limiter``'s slot -- rather than building a second
        limiter -- is what keeps the ceiling a ceiling: a private bucket would
        cap manual fetches on their own while doubling the real outbound rate
        against fanart.tv / Spotify / Deezer, the way the artist-art backfill
        daemon's separate instance already does.
        """
        return self._limiter.slot()

    async def get_artist_image(
        self, name: str, *, get_mbid: Callable[[], str | None] | None = None
    ) -> tuple[bytes, str] | None:
        if not self._is_enabled():
            return None

        # The cache read is a blocking disk read (up to 10 MB); offload it so a
        # cache hit never stalls the event loop. The network resolve below stays
        # on the loop (it is already async).
        cached = await run_in_threadpool(self._cache.get, name)
        if isinstance(cached, CachedImage):
            return (cached.data, cached.content_type)
        if cached is NEGATIVE:
            return None

        # Cache miss: resolve the MBID lazily (only now), then resolve under the
        # limiter. get_mbid runs a synchronous beets query, so offload it too.
        mbid = await run_in_threadpool(get_mbid) if get_mbid is not None else None
        try:
            async with self._limiter.slot():
                resolved = await self._source.resolve(name, mbid=mbid)
        except TransientSourceError:
            self._cache.store_negative(name, ttl_seconds=self._transient_ttl_seconds)
            return None

        if resolved is None:
            self._cache.store_negative(name, ttl_seconds=self._negative_ttl_seconds)
            return None

        self._cache.store_positive(name, resolved.data, resolved.content_type)
        return (resolved.data, resolved.content_type)
