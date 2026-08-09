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
    *,
    cache: ArtistImageCache,
    client: httpx.AsyncClient,
    enabled: bool = True,
    negative_ttl_seconds: float = 3600,
    transient_ttl_seconds: float = 600,
) -> ArtistImageService:
    source = DeezerArtistImageSource(client=client, search_limit=5)
    limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2)
    return ArtistImageService(
        source=source,
        cache=cache,
        limiter=limiter,
        is_enabled=lambda: enabled,
        negative_ttl_seconds=negative_ttl_seconds,
        transient_ttl_seconds=transient_ttl_seconds,
    )


@pytest.fixture
def cache(tmp_path: Path) -> ArtistImageCache:
    return ArtistImageCache(tmp_path)


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
async def test_transient_429_negative_cached_with_short_ttl(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
    service = _make_service(cache=cache, client=client, transient_ttl_seconds=600)

    assert await service.get_artist_image("ABBA") is None
    # Within the short TTL: a second call does not re-hit Deezer.
    assert await service.get_artist_image("ABBA") is None
    assert search.call_count == 1

    # The transient marker carries the SHORT ttl (not the 7-day confirmed one):
    # its stored expiry is ~now+600, well below the confirmed default.
    import time

    key = cache._key("ABBA")
    expiry = float((tmp_miss := cache._dir / f"{key}.miss").read_text().strip())
    assert tmp_miss.exists()
    assert expiry == pytest.approx(time.time() + 600, abs=30)


@pytest.mark.anyio
@respx.mock
async def test_transient_re_resolves_after_short_ttl_lapses(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    # transient_ttl=0 -> marker is immediately stale -> the next call re-tries
    # Deezer (a confirmed no-match would NOT, with its long TTL).
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
    service = _make_service(cache=cache, client=client, transient_ttl_seconds=0)

    assert await service.get_artist_image("ABBA") is None
    assert await service.get_artist_image("ABBA") is None
    assert search.call_count == 2  # stale transient marker -> retried


@pytest.mark.anyio
@respx.mock
async def test_confirmed_no_match_uses_long_ttl(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    # A confirmed no-match with negative_ttl=0 would re-resolve, but with the
    # long TTL it stays cached. Contrast a transient_ttl=0 service to prove the
    # confirmed path reads negative_ttl, not transient_ttl.
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": []}))
    service = _make_service(
        cache=cache, client=client, negative_ttl_seconds=3600, transient_ttl_seconds=0
    )

    assert await service.get_artist_image("Nobody") is None
    assert await service.get_artist_image("Nobody") is None
    # Confirmed path used the LONG ttl despite transient_ttl=0 -> still cached.
    assert search.call_count == 1


@pytest.mark.anyio
@respx.mock
async def test_malformed_body_is_transient_no_download(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, content=b"<html>"))
    img = respx.get(url__regex=r"https://img/.*")
    service = _make_service(cache=cache, client=client, transient_ttl_seconds=600)

    assert await service.get_artist_image("ABBA") is None
    assert not img.called
    # Cached as transient (short ttl): second call does not re-hit within TTL.
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
        async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return None

    limiter = TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2)
    service = ArtistImageService(
        source=_SlowSource(),
        cache=cache,
        limiter=limiter,
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )

    await asyncio.gather(*(service.get_artist_image(f"artist-{i}") for i in range(6)))
    assert peak <= 2


@pytest.mark.anyio
@respx.mock
async def test_is_enabled_callable_consulted_per_call(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=1, nb_album=1, picture_xl="https://img/a.jpg")]
            },
        )
    )
    respx.get("https://img/a.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    flag = {"on": False}
    service = ArtistImageService(
        source=DeezerArtistImageSource(client=client, search_limit=5),
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: flag["on"],
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )
    assert await service.get_artist_image("ABBA") is None  # off
    flag["on"] = True
    assert await service.get_artist_image("ABBA") == (b"IMG", "image/jpeg")  # on, no rebuild


@pytest.mark.anyio
@respx.mock
async def test_get_mbid_called_only_on_cache_miss(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_hit(name="ABBA", nb_fan=1, nb_album=1, picture_xl="https://img/a.jpg")]
            },
        )
    )
    respx.get("https://img/a.jpg").mock(
        return_value=httpx.Response(200, content=b"IMG", headers={"content-type": "image/jpeg"})
    )
    calls = {"n": 0}

    def get_mbid() -> str | None:
        calls["n"] += 1
        return "the-mbid"

    service = _make_service(cache=cache, client=client)
    # Cache miss -> resolve -> get_mbid invoked once.
    assert await service.get_artist_image("ABBA", get_mbid=get_mbid) == (b"IMG", "image/jpeg")
    assert calls["n"] == 1
    # Cache hit -> short-circuit -> get_mbid NOT invoked again.
    assert await service.get_artist_image("ABBA", get_mbid=get_mbid) == (b"IMG", "image/jpeg")
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_is_enabled_reflects_the_injected_predicate(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    flag = {"on": False}
    service = ArtistImageService(
        source=DeezerArtistImageSource(client=client, search_limit=5),
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=2),
        is_enabled=lambda: flag["on"],
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )
    assert service.is_enabled() is False
    flag["on"] = True
    assert service.is_enabled() is True


@pytest.mark.anyio
async def test_limiter_slot_caps_concurrent_manual_fetches(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    # A manual fetch must pace against a bucket at all -- six at once may not
    # all go out together. (That it is the SAME bucket the chain uses is a
    # different claim, pinned by the test below.)
    import asyncio

    active = 0
    peak = 0
    service = _make_service(cache=cache, client=client)

    async def borrow() -> None:
        nonlocal active, peak
        async with service.limiter_slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(borrow() for _ in range(6)))
    assert peak <= 2


@pytest.mark.anyio
async def test_limiter_slot_shares_the_bucket_the_resolve_chain_uses(
    cache: ArtistImageCache, client: httpx.AsyncClient
) -> None:
    """The manual slot has to contend with the CHAIN, not merely with itself.

    Peak-concurrency alone cannot see the failure this guards: a service that
    handed out slots from a second private ``TokenBucketLimiter`` would still
    cap manual fetches at 2 and pass that assertion, while doubling the real
    outbound rate against fanart.tv / Spotify / Deezer. Cross-path contention is
    the only observable difference -- with ONE bucket at ``max_concurrency=1``,
    a resolve holding the slot leaves ``limiter_slot()`` waiting.

    No wall clock is load-bearing here: the manual path does no I/O and no
    threadpool hop, so a second bucket would be acquired within one loop
    iteration and the sleep is pure headroom.
    """
    import asyncio

    chain_holding = asyncio.Event()
    let_chain_finish = asyncio.Event()
    manual_entered = asyncio.Event()

    class _HoldingSource:
        async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
            chain_holding.set()
            await let_chain_finish.wait()
            return None

    service = ArtistImageService(
        source=_HoldingSource(),
        cache=cache,
        limiter=TokenBucketLimiter(rate_per_sec=1000.0, max_concurrency=1),
        is_enabled=lambda: True,
        negative_ttl_seconds=3600,
        transient_ttl_seconds=600,
    )

    chain = asyncio.ensure_future(service.get_artist_image("ABBA"))
    await chain_holding.wait()  # the chain now owns the only slot

    async def manual() -> None:
        async with service.limiter_slot():
            manual_entered.set()

    manual_task = asyncio.ensure_future(manual())
    await asyncio.sleep(0.05)
    blocked = not manual_entered.is_set()

    let_chain_finish.set()
    assert await chain is None
    await manual_task

    assert blocked, "limiter_slot() acquired while the resolve chain held the only slot"
    assert manual_entered.is_set()  # and it is released, not deadlocked
