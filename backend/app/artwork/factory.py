"""Build the artist-image source chain from settings.

Shared by the app lifespan AND the artist-art backfill worker (which runs in its
own event loop and must build its own chain over its own httpx client) — so this
lives in app/artwork/, NOT app/main.py (avoids main↔runner coupling).
"""

from __future__ import annotations

import httpx

from app.artwork.chained import ChainedArtistImageSource
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.fanart import FanartTvArtistImageSource
from app.artwork.source import ArtistImageSource
from app.artwork.spotify import SpotifyArtistImageSource
from app.config import Settings


def build_source_chain(client: httpx.AsyncClient, settings: Settings) -> ArtistImageSource:
    sources: list[ArtistImageSource] = []
    if settings.artist_image_fanarttv_api_key:
        sources.append(
            FanartTvArtistImageSource(
                client=client,
                api_key=settings.artist_image_fanarttv_api_key,
                client_key=settings.artist_image_fanarttv_client_key,
            )
        )
    if settings.artist_image_spotify_client_id and settings.artist_image_spotify_client_secret:
        sources.append(
            SpotifyArtistImageSource(
                client=client,
                client_id=settings.artist_image_spotify_client_id,
                client_secret=settings.artist_image_spotify_client_secret,
                search_limit=settings.artist_image_search_limit,
            )
        )
    sources.append(
        DeezerArtistImageSource(client=client, search_limit=settings.artist_image_search_limit)
    )
    return ChainedArtistImageSource(sources)


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
