# tests/test_build_artist_image_service.py
from pathlib import Path

import httpx
import pytest

import app.main as main_mod
from app.artwork.cache import ArtistImageCache
from app.artwork.chained import ChainedArtistImageSource
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.fanart import FanartTvArtistImageSource
from app.artwork.service import ArtistImageService
from app.artwork.spotify import SpotifyArtistImageSource
from app.artwork.toggle import ArtistImageToggle
from app.config import settings

# `app.main` binds the same `settings` singleton instance (`from app.config
# import settings`), so monkeypatching this object is visible to
# `_build_artist_image_service`. We patch via the source module to keep
# mypy --strict happy about explicit re-exports.

Parts = tuple[httpx.AsyncClient, ArtistImageCache, ArtistImageToggle]


@pytest.fixture
def parts(tmp_path: Path) -> Parts:
    client = httpx.AsyncClient()
    cache = ArtistImageCache(tmp_path)
    toggle = ArtistImageToggle(tmp_path / "_enabled.json", default=False)
    return client, cache, toggle


def _chain_types(service: ArtistImageService) -> list[type]:
    source = service._source
    assert isinstance(source, ChainedArtistImageSource)
    return [type(s) for s in source._sources]


def test_deezer_only_when_no_credentials(parts: Parts, monkeypatch: pytest.MonkeyPatch) -> None:
    client, cache, toggle = parts
    monkeypatch.setattr(settings, "artist_image_fanarttv_api_key", "")
    monkeypatch.setattr(settings, "artist_image_spotify_client_id", "")
    monkeypatch.setattr(settings, "artist_image_spotify_client_secret", "")
    service = main_mod._build_artist_image_service(client, cache, toggle)
    assert _chain_types(service) == [DeezerArtistImageSource]


def test_full_chain_order_when_all_configured(
    parts: Parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cache, toggle = parts
    monkeypatch.setattr(settings, "artist_image_fanarttv_api_key", "K")
    monkeypatch.setattr(settings, "artist_image_spotify_client_id", "ID")
    monkeypatch.setattr(settings, "artist_image_spotify_client_secret", "SEC")
    service = main_mod._build_artist_image_service(client, cache, toggle)
    assert _chain_types(service) == [
        FanartTvArtistImageSource,
        SpotifyArtistImageSource,
        DeezerArtistImageSource,
    ]


def test_spotify_skipped_without_both_creds(parts: Parts, monkeypatch: pytest.MonkeyPatch) -> None:
    client, cache, toggle = parts
    monkeypatch.setattr(settings, "artist_image_fanarttv_api_key", "")
    monkeypatch.setattr(settings, "artist_image_spotify_client_id", "ID")
    monkeypatch.setattr(settings, "artist_image_spotify_client_secret", "")  # no secret
    service = main_mod._build_artist_image_service(client, cache, toggle)
    assert _chain_types(service) == [DeezerArtistImageSource]
