# tests/test_build_artist_image_service.py
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import app.main as main_mod
from app.api.artists import get_artist_image_sources
from app.artwork.cache import ArtistImageCache
from app.artwork.chained import ChainedArtistImageSource
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.factory import ArtistImageSources, build_artist_image_sources
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


def _chain_of(service: ArtistImageService) -> ChainedArtistImageSource:
    source = service._source
    assert isinstance(source, ChainedArtistImageSource)
    return source


def _request_for(app: FastAPI) -> Request:
    """The minimum ASGI scope ``get_artist_image_sources`` reads (``request.app``)."""
    return Request({"type": "http", "app": app, "headers": []})


def test_deezer_only_when_no_credentials(parts: Parts, monkeypatch: pytest.MonkeyPatch) -> None:
    client, cache, toggle = parts
    monkeypatch.setattr(settings, "artist_image_fanarttv_api_key", "")
    monkeypatch.setattr(settings, "artist_image_spotify_client_id", "")
    monkeypatch.setattr(settings, "artist_image_spotify_client_secret", "")
    service = main_mod._build_artist_image_service(
        cache, toggle.is_enabled, build_artist_image_sources(client, settings)
    )
    assert _chain_types(service) == [DeezerArtistImageSource]


def test_full_chain_order_when_all_configured(
    parts: Parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, cache, toggle = parts
    monkeypatch.setattr(settings, "artist_image_fanarttv_api_key", "K")
    monkeypatch.setattr(settings, "artist_image_spotify_client_id", "ID")
    monkeypatch.setattr(settings, "artist_image_spotify_client_secret", "SEC")
    service = main_mod._build_artist_image_service(
        cache, toggle.is_enabled, build_artist_image_sources(client, settings)
    )
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
    service = main_mod._build_artist_image_service(
        cache, toggle.is_enabled, build_artist_image_sources(client, settings)
    )
    assert _chain_types(service) == [DeezerArtistImageSource]


def test_service_chain_holds_the_registry_instances(
    parts: Parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The service must WRAP the registry's objects, not construct its own - a
    # second Spotify instance would carry a second OAuth token cache.
    client, cache, toggle = parts
    monkeypatch.setattr(settings, "artist_image_fanarttv_api_key", "K")
    monkeypatch.setattr(settings, "artist_image_spotify_client_id", "ID")
    monkeypatch.setattr(settings, "artist_image_spotify_client_secret", "SEC")
    sources = build_artist_image_sources(client, settings)
    service = main_mod._build_artist_image_service(cache, toggle.is_enabled, sources)
    assert _chain_of(service)._sources[1] is sources.get("spotify")


def test_lifespan_exposes_one_registry_shared_with_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One construction per app: the chain, ``app.state`` and the API dependency
    must all address the SAME source objects.

    Identity, not shape, is the assertion that matters: a lifespan that built a
    second set (``build_source_chain(client, settings)`` for the service and
    ``build_artist_image_sources(...)`` for the registry) produces an identical
    LOOKING chain of the same classes, while Spotify's ``_token`` cache silently
    exists twice. Only ``is`` sees that.
    """
    music = tmp_path / "music"
    music.mkdir()
    (tmp_path / "config.yaml").write_text(f"directory: {music}\nlibrary: library.db\nplugins: []\n")
    # beets_dir comes from backend/.env on a dev box and points at the REAL
    # library; the lifespan opens it for real, so pin it at a temp dir.
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    monkeypatch.setattr(settings, "artist_image_cache_dir", str(tmp_path / "cache"))
    app = main_mod.app
    with TestClient(app):  # context-manager form runs the lifespan
        sources = app.state.artist_image_sources
        assert isinstance(sources, ArtistImageSources)
        chain = _chain_of(app.state.artist_image_service)
        registry_sources = [source for _, source in sources.ordered]
        assert len(chain._sources) == len(registry_sources)
        assert all(a is b for a, b in zip(chain._sources, registry_sources, strict=True))
        # ...and the registry the API addresses is that same one, not a fallback.
        assert get_artist_image_sources(_request_for(app)) is sources


def test_lifespan_teardown_drops_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown must leave no registry behind, or the fallback is unreachable.

    ``main_mod.app`` is a module singleton, so an attribute set in the lifespan
    outlives it for the whole session. A leaked registry holds sources bound to
    the CLOSED httpx client, and it SHADOWS the fallback: a later bare
    ``TestClient(app)`` route test would get the stale registry and fail with
    ``RuntimeError: Cannot send a request, as the client has been closed`` -
    raised before the transport, so a respx mock cannot mask it. Same hazard the
    teardown already documents for ``event_broker``.

    Reading the registry INSIDE the block is what makes this a teardown test:
    without it, "no attribute afterwards" would also pass if the lifespan had
    never set one.
    """
    music = tmp_path / "music"
    music.mkdir()
    (tmp_path / "config.yaml").write_text(f"directory: {music}\nlibrary: library.db\nplugins: []\n")
    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))
    monkeypatch.setattr(settings, "artist_image_cache_dir", str(tmp_path / "cache"))
    app = main_mod.app
    with TestClient(app):
        built = app.state.artist_image_sources
        assert isinstance(built, ArtistImageSources)
    assert not hasattr(app.state, "artist_image_sources")
    # ...so the dependency reaches the fallback rather than the dead registry.
    assert get_artist_image_sources(_request_for(app)) is not built


def test_get_artist_image_sources_falls_back_without_a_lifespan() -> None:
    # TestClient(app) skips the lifespan, so the dependency must still yield a
    # usable registry rather than raising AttributeError on app.state.
    sources = get_artist_image_sources(_request_for(FastAPI()))
    assert isinstance(sources, ArtistImageSources)
    assert "deezer" in sources.ids()
