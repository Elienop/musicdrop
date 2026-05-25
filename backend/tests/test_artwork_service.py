from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.artwork.cache import ArtistImageCache
from app.artwork.deezer import DeezerArtistImageSource
from app.artwork.rate_limit import TokenBucketLimiter
from app.artwork.service import ArtistImageService
from app.artwork.source import ResolvedImage

SEARCH_URL = "https://api.deezer.com/search/artist"


def _hit(*, name: str, nb_fan: int, nb_album: int, picture_xl: str) -> dict[str, Any]:
    return {
        "name": name,
        "nb_fan": nb_fan,
        "nb_album": nb_album,
        "picture_xl": picture_xl,
        "picture_big": "",
    }


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


def _make_service(
    *, cache: ArtistImageCache, client: httpx.AsyncClient, enabled: bool = True
) -> ArtistImageService:
    source = DeezerArtistImageSource(client=client, search_limit=5)
    limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2)
    return ArtistImageService(source=source, cache=cache, limiter=limiter, enabled=enabled)


@pytest.fixture
def cache(tmp_path: Path) -> ArtistImageCache:
    return ArtistImageCache(tmp_path, negative_ttl_seconds=3600)


@pytest.mark.anyio
@respx.mock
async def test_resolves_caches_and_second_call_skips_http(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    search = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=100, nb_album=8, picture_xl="https://img/a.jpg")]
            },
        )
    )
    img = respx.get("https://img/a.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    service = _make_service(cache=cache, client=client)

    first = await service.get_artist_image("ABBA")
    assert first == (b"IMG", "image/jpeg")
    assert search.call_count == 1
    assert img.call_count == 1

    second = await service.get_artist_image("ABBA")
    assert second == (b"IMG", "image/jpeg")
    # Served from cache: no further HTTP.
    assert search.call_count == 1
    assert img.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_near_match_negative_cached_no_repeat_http(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    search = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _hit(
                        name="The Beatles", nb_fan=999, nb_album=20, picture_xl="https://img/b.jpg"
                    )
                ]
            },
        )
    )
    img = respx.get("https://img/b.jpg")
    service = _make_service(cache=cache, client=client)

    assert await service.get_artist_image("Beatles") is None
    assert not img.called  # never downloaded
    assert search.call_count == 1

    assert await service.get_artist_image("Beatles") is None
    assert search.call_count == 1  # negative cache honored


@pytest.mark.anyio
@respx.mock
async def test_empty_results_negative_cached_no_repeat_http(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    service = _make_service(cache=cache, client=client)

    assert await service.get_artist_image("Nobody") is None
    assert await service.get_artist_image("Nobody") is None
    assert search.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_override_served_with_zero_http(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    cache.write_override("ABBA", b"MANUAL", "image/png")
    search = respx.get(SEARCH_URL)
    service = _make_service(cache=cache, client=client)

    result = await service.get_artist_image("ABBA")
    assert result == (b"MANUAL", "image/png")
    assert not search.called


@pytest.mark.anyio
@respx.mock
async def test_disabled_returns_none_no_http(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    search = respx.get(SEARCH_URL)
    service = _make_service(cache=cache, client=client, enabled=False)

    assert await service.get_artist_image("ABBA") is None
    assert not search.called


@pytest.mark.anyio
@respx.mock
async def test_transient_429_negative_cached(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
    service = _make_service(cache=cache, client=client)

    assert await service.get_artist_image("ABBA") is None
    # Negative cached: a second call within TTL does not re-hit Deezer.
    assert await service.get_artist_image("ABBA") is None
    assert search.call_count == 1


@pytest.mark.anyio
async def test_resolution_runs_under_the_rate_limiter(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    # Use a fake source that records peak concurrency; cap is 2.
    import asyncio

    active = 0
    peak = 0

    class _SlowSource:
        async def resolve(self, name: str) -> ResolvedImage | None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return None

    limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2)
    service = ArtistImageService(source=_SlowSource(), cache=cache, limiter=limiter, enabled=True)

    await asyncio.gather(*(service.get_artist_image(f"artist-{i}") for i in range(6)))
    assert peak <= 2
