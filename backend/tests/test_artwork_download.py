from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.artwork.download import download_image
from app.artwork.images import MAX_IMAGE_BYTES
from app.artwork.source import ResolvedImage, TransientSourceError


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


@pytest.mark.anyio
@respx.mock
async def test_download_ok(client: httpx.AsyncClient) -> None:
    respx.get("https://img/x.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    assert await download_image(client, "https://img/x.jpg") == ResolvedImage(
        data=b"IMG", content_type="image/jpeg"
    )


@pytest.mark.anyio
@respx.mock
async def test_download_non_image_is_transient(client: httpx.AsyncClient) -> None:
    respx.get("https://img/x").mock(
        return_value=httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    )
    with pytest.raises(TransientSourceError):
        await download_image(client, "https://img/x")


@pytest.mark.anyio
@respx.mock
async def test_download_oversize_content_length_is_transient(client: httpx.AsyncClient) -> None:
    respx.get("https://img/big").mock(
        return_value=httpx.Response(
            200,
            content=b"x",
            headers={"content-type": "image/jpeg", "content-length": str(MAX_IMAGE_BYTES + 1)},
        )
    )
    with pytest.raises(TransientSourceError):
        await download_image(client, "https://img/big")


@pytest.mark.anyio
@respx.mock
async def test_download_http_error_is_transient(client: httpx.AsyncClient) -> None:
    respx.get("https://img/500").mock(return_value=httpx.Response(500))
    with pytest.raises(TransientSourceError):
        await download_image(client, "https://img/500")


@pytest.mark.anyio
@respx.mock
async def test_download_follows_redirects(client: httpx.AsyncClient) -> None:
    # A CDN 302 must be FOLLOWED, not treated as a download failure.
    respx.get("https://cdn/src.jpg").mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn/final.jpg"})
    )
    respx.get("https://cdn/final.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    assert await download_image(client, "https://cdn/src.jpg") == ResolvedImage(
        data=b"IMG", content_type="image/jpeg"
    )


@pytest.mark.anyio
@respx.mock
async def test_download_rejects_placeholder_url(client: httpx.AsyncClient) -> None:
    # Deezer 302-redirects a no-photo artist to its blank-avatar placeholder
    # (hash = MD5 of the empty string). That is a confirmed no-image, not a
    # transient failure -> None.
    blank = (
        "https://cdn-images.dzcdn.net/images/artist/"
        "d41d8cd98f00b204e9800998ecf8427e/1000x1000-000000-80-0-0.jpg"
    )
    src = "https://cdn-images.dzcdn.net/images/artist/realhash/1000x1000-000000-80-0-0.jpg"
    respx.get(src).mock(return_value=httpx.Response(302, headers={"location": blank}))
    respx.get(blank).mock(
        return_value=httpx.Response(200, content=b"BLANK", headers={"content-type": "image/jpeg"})
    )
    result = await download_image(
        client, src, reject_url_substrings=("d41d8cd98f00b204e9800998ecf8427e",)
    )
    assert result is None
