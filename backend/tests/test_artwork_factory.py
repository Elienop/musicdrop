import httpx
import pytest

from app.artwork import factory
from app.artwork.chained import ChainedArtistImageSource
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.factory import (
    build_artist_image_sources,
    build_fanart_background_source,
    build_source_chain,
    label_for,
)
from app.config import Settings


def _settings(*, artist_image_fanarttv_api_key: str = "") -> Settings:
    # env-free; defaults = no keys
    return Settings(artist_image_fanarttv_api_key=artist_image_fanarttv_api_key)


def test_chain_is_deezer_only_without_keys() -> None:
    client = httpx.AsyncClient()
    chain = build_source_chain(client, _settings())
    assert isinstance(chain, ChainedArtistImageSource)
    assert [type(s) for s in chain._sources] == [DeezerArtistImageSource]


def test_background_source_none_without_fanart_key() -> None:
    assert build_fanart_background_source(httpx.AsyncClient(), _settings()) is None


def test_background_source_present_with_fanart_key() -> None:
    from app.artwork.fanart import FanartTvArtistImageSource

    src = build_fanart_background_source(
        httpx.AsyncClient(), _settings(artist_image_fanarttv_api_key="K")
    )
    assert isinstance(src, FanartTvArtistImageSource)


def _all_configured() -> Settings:
    return Settings(
        artist_image_fanarttv_api_key="K",
        artist_image_spotify_client_id="ID",
        artist_image_spotify_client_secret="SEC",
    )


def test_sources_ids_are_deezer_only_without_keys() -> None:
    sources = build_artist_image_sources(httpx.AsyncClient(), _settings())
    assert sources.ids() == ("deezer",)


def test_sources_ids_are_chain_ordered_when_all_configured() -> None:
    sources = build_artist_image_sources(httpx.AsyncClient(), _all_configured())
    assert sources.ids() == ("fanarttv", "spotify", "deezer")


def test_chain_wraps_the_very_same_instances_get_returns() -> None:
    # The whole point of the registry: a manual per-source fetch must reuse the
    # object the automatic chain holds, or Spotify gets a second token cache.
    sources = build_artist_image_sources(httpx.AsyncClient(), _all_configured())
    chain = sources.chain()
    assert isinstance(chain, ChainedArtistImageSource)
    assert chain._sources[0] is sources.get("fanarttv")
    assert chain._sources[1] is sources.get("spotify")
    assert chain._sources[2] is sources.get("deezer")
    assert len(chain._sources) == 3


def test_build_source_chain_delegates_to_the_one_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wiring, not style: if the backfill worker's chain were built by a SECOND
    # construction, a change to which sources are configured (or to how they
    # are) would land on one path and not the other.
    real = factory.build_artist_image_sources
    seen: list[Settings] = []

    def spy(client: httpx.AsyncClient, settings: Settings) -> factory.ArtistImageSources:
        seen.append(settings)
        return real(client, settings)

    monkeypatch.setattr(factory, "build_artist_image_sources", spy)
    settings = _all_configured()
    chain = factory.build_source_chain(httpx.AsyncClient(), settings)
    assert isinstance(chain, ChainedArtistImageSource)
    assert len(seen) == 1
    assert seen[0] is settings


def test_get_returns_none_for_an_unconfigured_source() -> None:
    sources = build_artist_image_sources(httpx.AsyncClient(), _settings())
    assert sources.get("spotify") is None
    assert sources.get("fanarttv") is None
    assert sources.get("nonsense") is None


def test_labels_are_the_display_names_the_ui_shows() -> None:
    assert label_for("fanarttv") == "fanart.tv"
    assert label_for("spotify") == "Spotify"
    assert label_for("deezer") == "Deezer"
    assert label_for("unknown") == "unknown"


def test_build_source_chain_still_returns_a_chain() -> None:
    # The backfill runner (app/artist_art_jobs/runner.py) calls this; its
    # signature and return type must not move.
    chain = build_source_chain(httpx.AsyncClient(), _settings())
    assert isinstance(chain, ChainedArtistImageSource)
