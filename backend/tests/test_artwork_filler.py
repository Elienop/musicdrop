import asyncio
from collections.abc import Callable

import pytest

from app.artwork.filler import ArtistImageFiller


class _CountingService:
    """Stand-in for ArtistImageService: counts resolves, optionally slow."""

    def __init__(
        self,
        result: tuple[bytes, str] | None = (b"IMG", "image/jpeg"),
        delay: float = 0.0,
        boom: bool = False,
    ) -> None:
        self.result = result
        self.delay = delay
        self.boom = boom
        self.calls: list[str] = []

    async def get_artist_image(
        self, name: str, *, get_mbid: Callable[[], str | None] | None = None
    ) -> tuple[bytes, str] | None:
        self.calls.append(name)
        if get_mbid is not None:
            get_mbid()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom:
            raise RuntimeError("beets blew up")
        return self.result


def _filler(
    flush_after: float = 0.01, max_wait: float = 0.05
) -> tuple[ArtistImageFiller, list[int]]:
    """A filler plus the list its notification callback appends to."""
    fired: list[int] = []
    filler = ArtistImageFiller(
        on_filled=lambda: fired.append(1), flush_after=flush_after, max_wait=max_wait
    )
    return filler, fired


@pytest.mark.anyio
async def test_a_quick_resolve_is_served_inline() -> None:
    service = _CountingService()
    filler, _fired = _filler()
    result = await filler.fill(service, "ABBA", get_mbid=lambda: None, grace_seconds=1.0)  # type: ignore[arg-type]  # _CountingService implements only get_artist_image
    assert result == (b"IMG", "image/jpeg")
    await filler.close()


@pytest.mark.anyio
async def test_a_slow_resolve_returns_none_and_keeps_running() -> None:
    service = _CountingService(delay=0.10)
    filler, _fired = _filler()
    result = await filler.fill(service, "ABBA", get_mbid=lambda: None, grace_seconds=0.01)  # type: ignore[arg-type]  # stub service
    assert result is None  # caller 404s now...
    assert filler.inflight_count() == 1  # ...and the fill is still going
    await asyncio.sleep(0.15)
    assert service.calls == ["ABBA"]
    assert filler.inflight_count() == 0
    await filler.close()


@pytest.mark.anyio
async def test_concurrent_requests_for_one_artist_resolve_once() -> None:
    # There is no single-flight anywhere else in the app; without this a cold
    # page would fire one upstream resolve per request per artist.
    service = _CountingService(delay=0.05)
    filler, _fired = _filler()
    results = await asyncio.gather(
        *(
            filler.fill(service, "ABBA", get_mbid=lambda: None, grace_seconds=1.0)  # type: ignore[arg-type]  # stub service
            for _ in range(5)
        )
    )
    assert service.calls == ["ABBA"]
    assert all(r == (b"IMG", "image/jpeg") for r in results)
    await filler.close()


@pytest.mark.anyio
async def test_twin_spellings_share_one_flight() -> None:
    # The cache keys on the NORMALIZED name, so the filler must too, or "ABBA"
    # and " abba " would resolve twice and write the same slot twice.
    service = _CountingService(delay=0.05)
    filler, _fired = _filler()
    await asyncio.gather(
        filler.fill(service, "ABBA", get_mbid=lambda: None, grace_seconds=1.0),  # type: ignore[arg-type]  # stub service
        filler.fill(service, "  abba ", get_mbid=lambda: None, grace_seconds=1.0),  # type: ignore[arg-type]  # stub service
    )
    assert len(service.calls) == 1
    await filler.close()


@pytest.mark.anyio
async def test_a_burst_of_fills_notifies_exactly_once() -> None:
    # ArtistImage keys its <img> on the GLOBAL asset version and useEventStream
    # bumps it un-debounced, so one emit per fill would remount every portrait
    # on the page once per portrait.
    service = _CountingService()
    filler, fired = _filler(flush_after=0.02, max_wait=1.0)
    await asyncio.gather(
        *(
            filler.fill(service, f"artist-{i}", get_mbid=lambda: None, grace_seconds=1.0)  # type: ignore[arg-type]  # stub service
            for i in range(12)
        )
    )
    await asyncio.sleep(0.10)
    assert fired == [1]
    await filler.close()


@pytest.mark.anyio
async def test_no_notification_when_nothing_was_found() -> None:
    service = _CountingService(result=None)
    filler, fired = _filler(flush_after=0.02)
    await filler.fill(service, "Nobody", get_mbid=lambda: None, grace_seconds=1.0)  # type: ignore[arg-type]  # stub service
    await asyncio.sleep(0.08)
    assert fired == []
    await filler.close()


@pytest.mark.anyio
async def test_a_raising_resolve_is_swallowed_and_clears_the_flight() -> None:
    # A background task that dies unobserved logs "Task exception was never
    # retrieved" and leaves the key wedged in the in-flight map forever.
    service = _CountingService(boom=True)
    filler, _fired = _filler()
    assert await filler.fill(service, "ABBA", get_mbid=lambda: None, grace_seconds=1.0) is None  # type: ignore[arg-type]  # stub service
    assert filler.inflight_count() == 0
    service.boom = False
    assert await filler.fill(service, "ABBA", get_mbid=lambda: None, grace_seconds=1.0) == (  # type: ignore[arg-type]  # stub service
        b"IMG",
        "image/jpeg",
    )
    await filler.close()


@pytest.mark.anyio
async def test_zero_grace_never_waits() -> None:
    service = _CountingService(delay=0.05)
    filler, _fired = _filler()
    assert await filler.fill(service, "ABBA", get_mbid=lambda: None, grace_seconds=0.0) is None  # type: ignore[arg-type]  # stub service
    assert filler.inflight_count() == 1
    await filler.close()


@pytest.mark.anyio
async def test_close_cancels_outstanding_fills() -> None:
    service = _CountingService(delay=5.0)
    filler, _fired = _filler()
    await filler.fill(service, "ABBA", get_mbid=lambda: None, grace_seconds=0.0)  # type: ignore[arg-type]  # stub service
    assert filler.inflight_count() == 1
    await filler.close()
    assert filler.inflight_count() == 0
