# tests/test_artist_art_runner.py
from pathlib import Path
from typing import Any

import pytest
from beets.library import Library

from app.artist_art_jobs.registry import ArtistArtBackfillRegistry
from app.artist_art_jobs.runner import sweep_async
from app.config import settings
from app.models.artist_art import ArtistArtOutcome


class _Lib:
    directory = b"/music"


@pytest.mark.anyio
async def test_default_fetch_one_skips_background_fetch_when_present(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-force sweep must NOT hit fanart.tv when every artist folder already has
    an artist-background.* — resolve_background is an uncached API call + full image
    download that write_artist_art would then discard (skip-existing)."""
    import app.artist_art_jobs.runner as runner
    from app.beets.artist_art import write_artist_art

    name = str(next(iter(edit_lib.albums())).albumartist)
    write_artist_art(
        edit_lib,
        name,
        poster=None,
        background=(b"\xff\xd8\xff\x00", "image/jpeg"),
        force=True,
        trash=None,  # nothing exists yet, so nothing is replaced
    )
    monkeypatch.setattr(runner, "get_artist_mbid", lambda lib, n: "mbid-123")  # reach the bg branch

    calls = {"bg": 0}

    class _Service:
        async def get_artist_image(self, n: str, *, get_mbid: Any) -> None:
            get_mbid()
            return None

    class _BgSource:
        async def resolve_background(self, mbid: str) -> None:
            calls["bg"] += 1
            return None

    service: Any = _Service()  # duck-typed stub for ArtistImageService
    bg_source: Any = _BgSource()
    await runner._default_fetch_one(
        service, bg_source, edit_lib, name, force=False, resolve_trash=None
    )
    assert calls["bg"] == 0  # skipped the fanart download (background already on disk)

    calls["bg"] = 0
    await runner._default_fetch_one(
        service, bg_source, edit_lib, name, force=True, resolve_trash=None
    )
    assert calls["bg"] == 1  # force re-fetches


@pytest.mark.anyio
async def test_the_trash_store_is_taken_on_the_worker_for_each_artist(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``checked_store_dirs`` is a per-call check, so the job asks again per
    artist instead of carrying one answer from the start of the run: a Trash dir
    swapped for a symlink into the library mid-run would otherwise send the rest
    of the run's replaced art into the library. A refused answer (``None``)
    refuses the folder — nothing written, the curated file where it was."""
    import app.artist_art_jobs.runner as runner
    from app.beets.artist_art import get_artist_dirs

    name = str(next(iter(edit_lib.albums())).albumartist)
    curated = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    for d in get_artist_dirs(edit_lib, name):
        (d / "artist-poster.png").write_bytes(curated)
    monkeypatch.setattr(runner, "get_artist_mbid", lambda lib, n: None)

    class _Poster:
        async def get_artist_image(self, n: str, *, get_mbid: Any) -> tuple[bytes, str]:
            return (b"\xff\xd8\xff new poster", "image/jpeg")

    asked: list[str] = []

    def resolve() -> None:
        asked.append(name)
        return None  # the store is refused right now

    service: Any = _Poster()  # duck-typed stub for ArtistImageService
    out = await runner._default_fetch_one(
        service, None, edit_lib, name, force=True, resolve_trash=resolve
    )

    assert asked == [name]  # asked on the worker, in the write's own thread
    assert (out.status, out.written) == ("failed", 0)
    for d in get_artist_dirs(edit_lib, name):
        assert (d / "artist-poster.png").read_bytes() == curated  # untouched
        assert not (d / "artist-poster.jpg").exists()


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
