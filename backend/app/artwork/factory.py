"""Build the artist-image sources from settings — as a chain, or addressed by id.

Shared by the app lifespan AND the artist-art backfill worker (which runs in its
own event loop and must build its own chain over its own httpx client) — so this
lives in app/artwork/, NOT app/main.py (avoids main↔runner coupling).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx

from app.artwork.chained import ChainedArtistImageSource
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.fanart import FanartTvArtistImageSource
from app.artwork.source import ArtistImageSource
from app.artwork.spotify import SpotifyArtistImageSource
from app.config import Settings

# Stable machine ids. These ARE the API contract (a Literal in
# app/models/artist.py, an enum in the generated TS), so they are frozen: a
# rename is a contract change. The label is what the UI and the X-Art-Source
# header show, and may be reworded freely.
FANARTTV: Final = "fanarttv"
SPOTIFY: Final = "spotify"
DEEZER: Final = "deezer"

_LABELS: Final[dict[str, str]] = {
    FANARTTV: "fanart.tv",
    SPOTIFY: "Spotify",
    DEEZER: "Deezer",
}


def label_for(source_id: str) -> str:
    """The human name for a source id; the id itself if it is unknown."""
    return _LABELS.get(source_id, source_id)


@dataclass(frozen=True)
class ArtistImageSources:
    """The configured artist-image sources, in chain order, addressable by id.

    ONE instance set backs both the automatic chain and the manual per-source
    fetch: ``chain()`` wraps exactly the objects ``get()`` returns. Building a
    second set instead would give Spotify a second client-credentials token
    cache (``SpotifyArtistImageSource`` holds ``_token``/``_token_expiry`` on
    the instance), so every manual fetch would pay an extra token round-trip
    and the two copies would expire independently.

    ``ordered`` is a tuple of ``(id, source)`` pairs rather than a dict because
    the ORDER is the chain's fallback order and must not depend on dict
    construction accidents.
    """

    ordered: tuple[tuple[str, ArtistImageSource], ...]

    def chain(self) -> ArtistImageSource:
        """The automatic first-verified-hit-wins chain over these sources."""
        return ChainedArtistImageSource([source for _, source in self.ordered])

    def get(self, source_id: str) -> ArtistImageSource | None:
        """One source by id, or ``None`` when it is not configured."""
        for candidate_id, source in self.ordered:
            if candidate_id == source_id:
                return source
        return None

    def ids(self) -> tuple[str, ...]:
        """The configured ids, in chain order."""
        return tuple(source_id for source_id, _ in self.ordered)


def build_artist_image_sources(client: httpx.AsyncClient, settings: Settings) -> ArtistImageSources:
    """Construct every source whose credentials are set, in chain order.

    Deezer is the keyless backstop and is always present; fanart.tv and Spotify
    join only when configured - exactly the gate ``build_source_chain`` has
    always applied, now exposed by id as well as wrapped in the chain.
    """
    sources: list[tuple[str, ArtistImageSource]] = []
    if settings.artist_image_fanarttv_api_key:
        sources.append(
            (
                FANARTTV,
                FanartTvArtistImageSource(
                    client=client,
                    api_key=settings.artist_image_fanarttv_api_key,
                    client_key=settings.artist_image_fanarttv_client_key,
                ),
            )
        )
    if settings.artist_image_spotify_client_id and settings.artist_image_spotify_client_secret:
        sources.append(
            (
                SPOTIFY,
                SpotifyArtistImageSource(
                    client=client,
                    client_id=settings.artist_image_spotify_client_id,
                    client_secret=settings.artist_image_spotify_client_secret,
                    search_limit=settings.artist_image_search_limit,
                ),
            )
        )
    sources.append(
        (
            DEEZER,
            DeezerArtistImageSource(client=client, search_limit=settings.artist_image_search_limit),
        )
    )
    return ArtistImageSources(ordered=tuple(sources))


def build_source_chain(client: httpx.AsyncClient, settings: Settings) -> ArtistImageSource:
    """The automatic chain. Kept as its own function because the artist-art
    backfill worker builds a chain (and nothing else) on its own event loop."""
    return build_artist_image_sources(client, settings).chain()


def build_fanart_background_source(
    client: httpx.AsyncClient, settings: Settings
) -> FanartTvArtistImageSource | None:
    """fanart.tv is the only background source; None when its key is unset."""
    if not settings.artist_image_fanarttv_api_key:
        return None
    return FanartTvArtistImageSource(
        client=client,
        api_key=settings.artist_image_fanarttv_api_key,
        client_key=settings.artist_image_fanarttv_client_key,
    )
