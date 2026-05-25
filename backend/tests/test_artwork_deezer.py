from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.source import ResolvedImage

SEARCH_URL = "https://api.deezer.com/search/artist"


def _hit(
    *, name: str, nb_fan: int, nb_album: int, picture_xl: str, picture_big: str = ""
) -> dict[str, Any]:
    return {
        "name": name,
        "nb_fan": nb_fan,
        "nb_album": nb_album,
        "picture_xl": picture_xl,
        "picture_big": picture_big,
    }


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def source(client: httpx.AsyncClient) -> DeezerArtistImageSource:
    return DeezerArtistImageSource(client=client, search_limit=5)


@pytest.mark.anyio
@respx.mock
async def test_exact_normalized_match_resolves(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _hit(name="ABBA", nb_fan=100, nb_album=8, picture_xl="https://img/abba_xl.jpg")
                ]
            },
        )
    )
    img_route = respx.get("https://img/abba_xl.jpg").mock(
        return_value=httpx.Response(
            200, content=b"JPEGDATA", headers={"content-type": "image/jpeg"}
        )
    )

    result = await source.resolve("ABBA")

    assert isinstance(result, ResolvedImage)
    assert result.data == b"JPEGDATA"
    assert result.content_type == "image/jpeg"
    assert img_route.called


@pytest.mark.anyio
@respx.mock
async def test_match_is_case_and_accent_insensitive(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="Beyoncé", nb_fan=9, nb_album=6, picture_xl="https://img/b.jpg")]
            },
        )
    )
    respx.get("https://img/b.jpg").mock(
        return_value=httpx.Response(200, content=b"X", headers={"content-type": "image/jpeg"})
    )

    result = await source.resolve("beyonce")
    assert result is not None


@pytest.mark.anyio
@respx.mock
async def test_near_match_rejected(source: DeezerArtistImageSource) -> None:
    # Query "Beatles" must NOT accept "The Beatles".
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _hit(
                        name="The Beatles", nb_fan=999, nb_album=20, picture_xl="https://img/tb.jpg"
                    )
                ]
            },
        )
    )
    img_route = respx.get("https://img/tb.jpg")

    result = await source.resolve("Beatles")
    assert result is None
    assert not img_route.called  # never downloaded the wrong artist


@pytest.mark.anyio
@respx.mock
async def test_empty_results_returns_none(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    assert await source.resolve("Nobody") is None


@pytest.mark.anyio
@respx.mock
async def test_picks_highest_prominence_among_verified(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _hit(
                        name="Genesis", nb_fan=10, nb_album=2, picture_xl="https://img/tribute.jpg"
                    ),
                    _hit(
                        name="Genesis", nb_fan=5000, nb_album=15, picture_xl="https://img/canon.jpg"
                    ),
                ]
            },
        )
    )
    canon = respx.get("https://img/canon.jpg").mock(
        return_value=httpx.Response(200, content=b"CANON", headers={"content-type": "image/jpeg"})
    )

    result = await source.resolve("Genesis")
    assert result is not None
    assert result.data == b"CANON"
    assert canon.called


@pytest.mark.anyio
@respx.mock
async def test_falls_back_to_picture_big(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _hit(
                        name="Tiny",
                        nb_fan=1,
                        nb_album=1,
                        picture_xl="",
                        picture_big="https://img/big.jpg",
                    )
                ]
            },
        )
    )
    big = respx.get("https://img/big.jpg").mock(
        return_value=httpx.Response(200, content=b"BIG", headers={"content-type": "image/png"})
    )

    result = await source.resolve("Tiny")
    assert result is not None
    assert result.data == b"BIG"
    assert big.called


@pytest.mark.anyio
@respx.mock
async def test_search_429_returns_none(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
    assert await source.resolve("ABBA") is None


@pytest.mark.anyio
@respx.mock
async def test_image_download_error_returns_none(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=100, nb_album=8, picture_xl="https://img/x.jpg")]
            },
        )
    )
    respx.get("https://img/x.jpg").mock(return_value=httpx.Response(500))
    assert await source.resolve("ABBA") is None


@pytest.mark.anyio
@respx.mock
async def test_no_picture_url_returns_none(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=100, nb_album=8, picture_xl="", picture_big="")]
            },
        )
    )
    assert await source.resolve("ABBA") is None
