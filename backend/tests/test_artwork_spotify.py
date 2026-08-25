from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx

from app.artwork.source import ResolvedImage, TransientSourceError
from app.artwork.spotify import SpotifyArtistImageSource

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


def _token(expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": "TOK", "token_type": "bearer", "expires_in": expires_in}
    )


def _artist(name: str, *, popularity: int = 50, url: str = "https://img/s.jpg") -> dict[str, Any]:
    return {
        "name": name,
        "popularity": popularity,
        "followers": {"total": 100},
        "images": [{"url": url, "height": 640, "width": 640}],
    }


def _source(
    client: httpx.AsyncClient, *, clock: list[float] | None = None
) -> SpotifyArtistImageSource:
    if clock is not None:
        now: Callable[[], float] = lambda: clock[0]  # noqa: E731
        return SpotifyArtistImageSource(
            client=client, client_id="ID", client_secret="SECRET", now=now
        )
    return SpotifyArtistImageSource(client=client, client_id="ID", client_secret="SECRET")


@pytest.mark.anyio
@respx.mock
async def test_resolves_verified_artist(client: httpx.AsyncClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token())
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"artists": {"items": [_artist("ABBA")]}})
    )
    respx.get("https://img/s.jpg").mock(
        return_value=httpx.Response(200, content=b"SP", headers={"content-type": "image/jpeg"})
    )
    assert await _source(client).resolve("ABBA") == ResolvedImage(
        data=b"SP", content_type="image/jpeg"
    )


@pytest.mark.anyio
@respx.mock
async def test_drops_fuzzy_non_match(client: httpx.AsyncClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token())
    # Spotify search is fuzzy; "Beatles" must NOT match "The Beatles".
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"artists": {"items": [_artist("The Beatles")]}})
    )
    assert await _source(client).resolve("Beatles") is None


@pytest.mark.anyio
@respx.mock
async def test_token_cached_across_calls(client: httpx.AsyncClient) -> None:
    token = respx.post(TOKEN_URL).mock(return_value=_token())
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"artists": {"items": []}}))
    src = _source(client)
    await src.resolve("Nobody")
    await src.resolve("Nobody")
    assert token.call_count == 1  # cached; not re-fetched


@pytest.mark.anyio
@respx.mock
async def test_token_refetched_after_expiry(client: httpx.AsyncClient) -> None:
    token = respx.post(TOKEN_URL).mock(return_value=_token(expires_in=100))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"artists": {"items": []}}))
    clock = [0.0]
    src = _source(client, clock=clock)
    await src.resolve("Nobody")
    clock[0] = 10_000.0  # well past expiry (100 - 60 skew)
    await src.resolve("Nobody")
    assert token.call_count == 2


@pytest.mark.anyio
@respx.mock
async def test_401_refreshes_token_and_retries(client: httpx.AsyncClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token())
    respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"artists": {"items": [_artist("ABBA")]}}),
        ]
    )
    respx.get("https://img/s.jpg").mock(
        return_value=httpx.Response(200, content=b"SP", headers={"content-type": "image/jpeg"})
    )
    assert await _source(client).resolve("ABBA") == ResolvedImage(
        data=b"SP", content_type="image/jpeg"
    )


@pytest.mark.anyio
@respx.mock
async def test_429_is_transient(client: httpx.AsyncClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=_token())
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
    source = _source(client)
    with pytest.raises(TransientSourceError):
        await source.resolve("ABBA")
