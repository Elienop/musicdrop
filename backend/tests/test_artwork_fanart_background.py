from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.artwork.fanart import FanartTvArtistImageSource
from app.artwork.source import ResolvedImage

MBID = "f4a31f0a-51dd-4fa7-986d-3095c40c5ed9"
ART = f"https://webservice.fanart.tv/v3/music/{MBID}"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.mark.anyio
async def test_no_mbid_returns_none(client: httpx.AsyncClient) -> None:
    assert (
        await FanartTvArtistImageSource(client=client, api_key="K").resolve_background(None) is None
    )


@pytest.mark.anyio
@respx.mock
async def test_picks_highest_likes_background(client: httpx.AsyncClient) -> None:
    respx.get(ART).mock(
        return_value=httpx.Response(
            200,
            json={
                "artistbackground": [
                    {"url": "https://img/lo.jpg", "likes": "1"},
                    {"url": "https://img/best.jpg", "likes": "7"},
                ]
            },
        )
    )
    respx.get("https://img/best.jpg").mock(
        return_value=httpx.Response(200, content=b"BG", headers={"content-type": "image/jpeg"})
    )
    src = FanartTvArtistImageSource(client=client, api_key="K")
    assert await src.resolve_background(MBID) == ResolvedImage(
        data=b"BG", content_type="image/jpeg"
    )


@pytest.mark.anyio
@respx.mock
async def test_no_background_returns_none(client: httpx.AsyncClient) -> None:
    respx.get(ART).mock(return_value=httpx.Response(200, json={"artistthumb": []}))
    assert (
        await FanartTvArtistImageSource(client=client, api_key="K").resolve_background(MBID) is None
    )
