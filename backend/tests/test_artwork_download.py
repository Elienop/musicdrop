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
@pytest.mark.parametrize(
    "declared",
    ["image/日本語".encode(), b"image/png\nX-Injected: yes"],
    ids=["non-ascii", "response-splitting"],
)
async def test_download_refuses_to_store_an_unsendable_content_type(
    client: httpx.AsyncClient, declared: bytes
) -> None:
    """The write end of the poisoned-slot hazard.

    ``startswith("image/")`` passes both of these — httpx decodes non-ASCII
    header BYTES as UTF-8 — and the value was then stored verbatim, so ONE
    hostile or broken CDN response permanently poisoned that artist's cache slot
    with something that 500s (non-ASCII) or drops the connection (newline) on
    every later request. The image is still an image, so it is kept under the
    generic type rather than benched as a failed download.

    The header is declared as raw bytes because httpx's own Response constructor
    ASCII-encodes a ``str`` value and would reject the hostile input outright —
    which is not what a real socket does.
    """
    respx.get("https://img/x.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers=[(b"content-type", declared)])
    )

    assert await download_image(client, "https://img/x.jpg") == ResolvedImage(
        data=b"IMG", content_type="application/octet-stream"
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


@pytest.mark.anyio
@respx.mock
async def test_fetch_image_bytes_returns_body(client: httpx.AsyncClient) -> None:
    from app.artwork.download import fetch_image_bytes

    respx.get("https://x/a.jpg").mock(
        return_value=httpx.Response(
            200, content=b"IMGBYTES", headers={"content-type": "image/jpeg"}
        )
    )
    assert await fetch_image_bytes(client, "https://x/a.jpg") == b"IMGBYTES"


@pytest.mark.anyio
@respx.mock
async def test_fetch_image_bytes_follows_redirect_and_ignores_content_type(
    client: httpx.AsyncClient,
) -> None:
    from app.artwork.download import fetch_image_bytes

    # 302 -> real bytes, served with a NON-image content-type (we trust the bytes).
    respx.get("https://x/src").mock(
        return_value=httpx.Response(302, headers={"location": "https://x/dst"})
    )
    respx.get("https://x/dst").mock(
        return_value=httpx.Response(
            200, content=b"OK", headers={"content-type": "application/octet-stream"}
        )
    )
    assert await fetch_image_bytes(client, "https://x/src") == b"OK"


@pytest.mark.anyio
@respx.mock
async def test_fetch_image_bytes_http_error_raises_valueerror(client: httpx.AsyncClient) -> None:
    from app.artwork.download import fetch_image_bytes

    respx.get("https://x/500").mock(return_value=httpx.Response(500))
    with pytest.raises(ValueError):
        await fetch_image_bytes(client, "https://x/500")


@pytest.mark.anyio
@respx.mock
async def test_fetch_image_bytes_oversize_content_length_raises(client: httpx.AsyncClient) -> None:
    from app.artwork.download import fetch_image_bytes

    respx.get("https://x/big").mock(
        return_value=httpx.Response(
            200, content=b"x", headers={"content-length": str(MAX_IMAGE_BYTES + 1)}
        )
    )
    with pytest.raises(ValueError):
        await fetch_image_bytes(client, "https://x/big")
