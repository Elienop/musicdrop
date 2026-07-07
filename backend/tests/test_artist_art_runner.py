# tests/test_artist_art_runner.py
from pathlib import Path

import pytest

from app.artist_art_jobs.registry import ArtistArtBackfillRegistry
from app.artist_art_jobs.runner import sweep_async
from app.config import settings
from app.models.artist_art import ArtistArtOutcome


class _Lib:
    directory = b"/music"


@pytest.mark.anyio
async def test_sweep_records_and_finishes() -> None:
    reg = ArtistArtBackfillRegistry()
    reg.start(force=True)
    seen: list[str] = []

    async def fake_fetch(name: str) -> ArtistArtOutcome:
        seen.append(name)
        return ArtistArtOutcome(artist=name, status="written", written=2, dirs=1)

    await sweep_async(
        reg,
        _Lib(),
        cache_dir=Path("/tmp/x"),
        settings=settings,
        delay=0,
        force=True,
        names=["A", "B"],
        fetch_one=fake_fetch,
    )
    assert seen == ["A", "B"]
    st = reg.state()
    assert (st.phase, st.processed, st.written) == ("done", 2, 2)


@pytest.mark.anyio
async def test_sweep_fires_on_complete_when_done() -> None:
    reg = ArtistArtBackfillRegistry()
    reg.start(force=True)
    fired: list[bool] = []

    async def fake_fetch(name: str) -> ArtistArtOutcome:
        return ArtistArtOutcome(artist=name, status="written", written=1, dirs=1)

    await sweep_async(
        reg,
        _Lib(),
        cache_dir=Path("/tmp/x"),
        settings=settings,
        delay=0,
        force=True,
        names=["A", "B"],
        fetch_one=fake_fetch,
        on_complete=lambda: fired.append(True),
    )
    assert reg.state().phase == "done"
    assert fired == [True]  # SSE repaint fired exactly once on completion


@pytest.mark.anyio
async def test_sweep_fires_on_complete_on_failure() -> None:
    reg = ArtistArtBackfillRegistry()
    reg.start(force=True)
    fired: list[bool] = []

    async def boom(name: str) -> ArtistArtOutcome:
        raise RuntimeError("kaboom")

    await sweep_async(
        reg,
        _Lib(),
        cache_dir=Path("/tmp/x"),
        settings=settings,
        delay=0,
        force=True,
        names=["A"],
        fetch_one=boom,
        on_complete=lambda: fired.append(True),
    )
    assert reg.state().phase == "failed"
    assert fired == [True]  # fires on the fail path too


@pytest.mark.anyio
async def test_sweep_honors_stop() -> None:
    reg = ArtistArtBackfillRegistry()
    reg.start(force=False)

    async def fake_fetch(name: str) -> ArtistArtOutcome:
        reg.request_stop()
        return ArtistArtOutcome(artist=name, status="skipped", written=0, dirs=1)

    await sweep_async(
        reg,
        _Lib(),
        cache_dir=Path("/tmp/x"),
        settings=settings,
        delay=0,
        force=False,
        names=["A", "B", "C"],
        fetch_one=fake_fetch,
    )
    assert reg.state().phase == "stopped"
    assert reg.state().processed == 1  # stopped after the first
