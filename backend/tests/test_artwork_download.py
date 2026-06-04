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
