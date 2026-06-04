import httpx

from app.artwork.chained import ChainedArtistImageSource
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.factory import build_fanart_background_source, build_source_chain
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
