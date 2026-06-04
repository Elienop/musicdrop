"""Try multiple artist-image sources in order; first verified hit wins.

A child raising :class:`TransientSourceError` is logged and skipped, so one
source's blip never aborts the chain. Returns ``None`` only when every child
confirms no-match; raises ``TransientSourceError`` if at least one child was
transient and none succeeded, so the service short-TTL-caches and retries soon
(rather than benching the artist for the 7-day confirmed-no-match window).
"""

from __future__ import annotations

import logging

from app.artwork.source import ArtistImageSource, ResolvedImage, TransientSourceError

_log = logging.getLogger("musicdrop.artwork")


class ChainedArtistImageSource:
    def __init__(self, sources: list[ArtistImageSource]) -> None:
        self._sources = sources

    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        had_transient = False
        for source in self._sources:
            try:
                result = await source.resolve(name, mbid=mbid)
            except TransientSourceError as exc:
                had_transient = True
                _log.warning("artist-image source %s transient: %s", type(source).__name__, exc)
                continue
            if result is not None:
                return result
        if had_transient:
            raise TransientSourceError("all artist-image sources failed or returned no match")
        return None
