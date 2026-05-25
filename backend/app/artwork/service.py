"""Artist-image orchestrator — the single entry point for callers.

``get_artist_image(name)`` resolution order:

1. **disabled?** -> ``None`` (no I/O at all).
2. **cache**: override -> serve; positive -> serve; fresh negative -> ``None``.
3. **resolve** via the source, under the rate limiter (paces the first-load
   burst). Verified -> store positive + serve. No match / transient error
   (incl. 429) -> store negative + ``None``.

Each artist therefore costs at most one network resolution; everything after
is served from disk until the negative TTL lapses.
"""

from app.artwork.cache import NEGATIVE, ArtistImageCache, CachedImage
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.source import ArtistImageSource


class ArtistImageService:
    def __init__(
        self,
        *,
        source: ArtistImageSource,
        cache: ArtistImageCache,
        limiter: TokenBucketLimiter,
        enabled: bool,
    ) -> None:
        self._source = source
        self._cache = cache
        self._limiter = limiter
        self._enabled = enabled

    async def get_artist_image(self, name: str) -> tuple[bytes, str] | None:
        if not self._enabled:
            return None

        cached = self._cache.get(name)
        if isinstance(cached, CachedImage):
            return (cached.data, cached.content_type)
        if cached is NEGATIVE:
            return None

        # Cache miss (or stale negative): resolve once, under the limiter.
        async with self._limiter.slot():
            resolved = await self._source.resolve(name)

        if resolved is None:
            self._cache.store_negative(name)
            return None

        self._cache.store_positive(name, resolved.data, resolved.content_type)
        return (resolved.data, resolved.content_type)
