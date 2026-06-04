from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.images import MAX_IMAGE_BYTES
from app.artwork.source import ResolvedImage, TransientSourceError

SEARCH_URL = "https://api.deezer.com/search/artist"


def _hit(
    *, name: object, nb_fan: int, nb_album: int, picture_xl: str, picture_big: str = ""
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
async def test_search_429_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")


@pytest.mark.anyio
@respx.mock
async def test_search_timeout_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")


@pytest.mark.anyio
@respx.mock
async def test_non_json_body_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, content=b"<html>error</html>"))
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")


@pytest.mark.anyio
@respx.mock
async def test_payload_not_dict_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=[1, 2, 3]))
    img = respx.get(url__regex=r"https://img/.*")
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")
    assert not img.called


@pytest.mark.anyio
@respx.mock
async def test_data_not_a_list_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": "nope"}))
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")


@pytest.mark.anyio
@respx.mock
async def test_image_download_error_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=100, nb_album=8, picture_xl="https://img/x.jpg")]
            },
        )
    )
    respx.get("https://img/x.jpg").mock(return_value=httpx.Response(500))
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")


@pytest.mark.anyio
@respx.mock
async def test_oversize_image_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=1, nb_album=1, picture_xl="https://img/x.jpg")]
            },
        )
    )
    big = b"\x00" * (MAX_IMAGE_BYTES + 1)
    respx.get("https://img/x.jpg").mock(
        return_value=httpx.Response(200, content=big, headers={"content-type": "image/jpeg"})
    )
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")


@pytest.mark.anyio
@respx.mock
async def test_oversize_content_length_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=1, nb_album=1, picture_xl="https://img/x.jpg")]
            },
        )
    )
    respx.get("https://img/x.jpg").mock(
        return_value=httpx.Response(
            200,
            content=b"small",
            headers={
                "content-type": "image/jpeg",
                "content-length": str(MAX_IMAGE_BYTES + 1),
            },
        )
    )
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")


@pytest.mark.anyio
@respx.mock
async def test_non_image_content_type_is_transient(source: DeezerArtistImageSource) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=1, nb_album=1, picture_xl="https://img/x.jpg")]
            },
        )
    )
    respx.get("https://img/x.jpg").mock(
        return_value=httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    )
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")


@pytest.mark.anyio
@respx.mock
async def test_no_picture_url_returns_none(source: DeezerArtistImageSource) -> None:
    # Confirmed: a valid search whose verified hit has no usable picture.
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=100, nb_album=8, picture_xl="", picture_big="")]
            },
        )
    )
    assert await source.resolve("ABBA") is None


@pytest.mark.anyio
@respx.mock
async def test_null_name_hit_does_not_match(source: DeezerArtistImageSource) -> None:
    # A JSON null name must not coerce to "none" and match a literal "None" query.
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name=None, nb_fan=999, nb_album=9, picture_xl="https://img/n.jpg")]
            },
        )
    )
    img = respx.get("https://img/n.jpg")
    assert await source.resolve("None") is None
    assert not img.called
