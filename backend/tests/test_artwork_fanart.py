from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.artwork.fanart import FanartTvArtistImageSource
from app.artwork.source import ResolvedImage, TransientSourceError

MBID = "f4a31f0a-51dd-4fa7-986d-3095c40c5ed9"
ART_URL = f"https://webservice.fanart.tv/v3/music/{MBID}"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


def _source(client: httpx.AsyncClient) -> FanartTvArtistImageSource:
    return FanartTvArtistImageSource(client=client, api_key="KEY")


@pytest.mark.anyio
async def test_no_mbid_returns_none(client: httpx.AsyncClient) -> None:
    assert await _source(client).resolve("ABBA", mbid=None) is None


@pytest.mark.anyio
@respx.mock
async def test_picks_highest_likes_thumb_and_downloads(client: httpx.AsyncClient) -> None:
    route = respx.get(ART_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "artistthumb": [
                    {"url": "https://img/low.jpg", "likes": "2"},
                    {"url": "https://img/best.jpg", "likes": "9"},
                ]
            },
        )
    )
    respx.get("https://img/best.jpg").mock(
        return_value=httpx.Response(200, content=b"BEST", headers={"content-type": "image/jpeg"})
    )
    result = await _source(client).resolve("ABBA", mbid=MBID)
    assert result == ResolvedImage(data=b"BEST", content_type="image/jpeg")
    # api-key header sent AND the shared client's defaults survive (no User-Agent
    # set on this bare client, but assert the auth header is present).
    assert route.calls.last.request.headers["api-key"] == "KEY"


@pytest.mark.anyio
@respx.mock
async def test_404_means_no_art_returns_none(client: httpx.AsyncClient) -> None:
    respx.get(ART_URL).mock(return_value=httpx.Response(404, json={"status": "error"}))
    assert await _source(client).resolve("ABBA", mbid=MBID) is None


@pytest.mark.anyio
@respx.mock
async def test_empty_artistthumb_returns_none(client: httpx.AsyncClient) -> None:
    respx.get(ART_URL).mock(return_value=httpx.Response(200, json={"artistbackground": []}))
    assert await _source(client).resolve("ABBA", mbid=MBID) is None


@pytest.mark.anyio
@respx.mock
async def test_401_is_transient(client: httpx.AsyncClient) -> None:
    respx.get(ART_URL).mock(return_value=httpx.Response(401))
    with pytest.raises(TransientSourceError):
        await _source(client).resolve("ABBA", mbid=MBID)


@pytest.mark.anyio
@respx.mock
async def test_client_key_header_sent_when_set(client: httpx.AsyncClient) -> None:
    route = respx.get(ART_URL).mock(return_value=httpx.Response(200, json={"artistthumb": []}))
    src = FanartTvArtistImageSource(client=client, api_key="KEY", client_key="PERSONAL")
    await src.resolve("ABBA", mbid=MBID)
    assert route.calls.last.request.headers["client-key"] == "PERSONAL"
