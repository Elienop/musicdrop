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

    async def get_artist_image(self, name: str) -> tuple[bytes, str] | None:
        if not self._is_enabled():
            return None

        cached = self._cache.get(name)
        if isinstance(cached, CachedImage):
            return (cached.data, cached.content_type)
        if cached is NEGATIVE:
            return None

        # Cache miss (or expired negative): resolve once, under the limiter.
        try:
            async with self._limiter.slot():
                resolved = await self._source.resolve(name)
        except TransientSourceError:
            self._cache.store_negative(name, ttl_seconds=self._transient_ttl_seconds)
            return None

        if resolved is None:
            self._cache.store_negative(name, ttl_seconds=self._negative_ttl_seconds)
            return None

        self._cache.store_positive(name, resolved.data, resolved.content_type)
        return (resolved.data, resolved.content_type)
