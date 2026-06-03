from __future__ import annotations

from typing import Any

import pytest
from beets.library import Library

from app.models.lyrics import ItemLyricsOutcome, ItemLyricsStatus


def test_backfill_status_model() -> None:
    from app.models.lyrics import LyricsBackfillStatus, LyricsCoverage

    cov = LyricsCoverage(total=10, with_lyrics=7, percent=70.0)
    assert cov.percent == 70.0

    status = LyricsBackfillStatus(
        phase="running",
        job_id="abc",
        total=10,
        processed=4,
        found=3,
        not_found=1,
        failed=0,
        skipped=0,
        current="Adele — 25 — Hello",
        writes_enabled=True,
        error=None,
        album_id=None,
        scope_label="library",
    )
    assert status.phase == "running"
    assert status.model_dump()["job_id"] == "abc"


def _outcome(status: ItemLyricsStatus) -> ItemLyricsOutcome:
    return ItemLyricsOutcome(item_id=1, status=status, source=None, written=False)


def test_registry_lifecycle_counts() -> None:
    from app.lyrics_jobs.registry import LyricsBackfillRegistry

    reg = LyricsBackfillRegistry()
    assert reg.state().phase == "idle"
    assert reg.is_running() is False

    reg.start(writes_enabled=True)
    assert reg.is_running() is True
    reg.set_total(3)
    reg.record(_outcome("found"))
    reg.record(_outcome("not_found"))
    reg.set_current("A — B — C")
    s = reg.state()
    assert (s.total, s.processed, s.found, s.not_found, s.current) == (3, 2, 1, 1, "A — B — C")

    reg.finish("done")
    assert reg.is_running() is False
    assert reg.state().phase == "done"


def test_registry_rejects_second_start() -> None:
    from app.lyrics_jobs.registry import LyricsBackfillRegistry

    reg = LyricsBackfillRegistry()
    reg.start(writes_enabled=True)
    with pytest.raises(RuntimeError):
        reg.start(writes_enabled=True)


def test_registry_stop_flag() -> None:
    from app.lyrics_jobs.registry import LyricsBackfillRegistry

    reg = LyricsBackfillRegistry()
    reg.start(writes_enabled=False)
    assert reg.should_stop() is False
    reg.request_stop()
    assert reg.should_stop() is True


def test_registry_singleton_accessors() -> None:
    from app.lyrics_jobs.registry import (
        get_lyrics_backfill,
        lyrics_backfill_active,
        reset_lyrics_backfill,
    )

    reset_lyrics_backfill()
    assert lyrics_backfill_active() is False
    get_lyrics_backfill().start(writes_enabled=True)
    assert lyrics_backfill_active() is True
    reset_lyrics_backfill()
    assert lyrics_backfill_active() is False


def test_sweep_processes_all_items_and_finishes_done(edit_lib: Library) -> None:
    from app.lyrics_jobs.registry import LyricsBackfillRegistry
    from app.lyrics_jobs.runner import sweep

    reg = LyricsBackfillRegistry()
    reg.start(writes_enabled=False)

    seen: list[int] = []

    def fake_fetch_one(plugin: Any, item: Any, *, force: bool, write: bool) -> ItemLyricsOutcome:
        seen.append(int(item.id))
        return _outcome("found")

    sweep(
        reg,
        edit_lib,
        delay=0.0,
        write=False,
        fetch_one=fake_fetch_one,
        make_plugin=lambda: object(),
    )

    s = reg.state()
    assert s.phase == "done"
    assert s.total == 3 and s.processed == 3 and s.found == 3
    assert len(seen) == 3


def test_sweep_honours_stop(edit_lib: Library) -> None:
    from app.lyrics_jobs.registry import LyricsBackfillRegistry
    from app.lyrics_jobs.runner import sweep

    reg = LyricsBackfillRegistry()
    reg.start(writes_enabled=False)

    def fake_fetch_one(plugin: Any, item: Any, *, force: bool, write: bool) -> ItemLyricsOutcome:
        reg.request_stop()  # stop after the first item
        return _outcome("found")

    sweep(
        reg,
        edit_lib,
        delay=0.0,
        write=False,
        fetch_one=fake_fetch_one,
        make_plugin=lambda: object(),
    )
    s = reg.state()
    assert s.phase == "stopped"
    assert s.processed == 1


def test_sweep_failure_sets_failed_phase(edit_lib: Library) -> None:
    from app.lyrics_jobs.registry import LyricsBackfillRegistry
    from app.lyrics_jobs.runner import sweep

    reg = LyricsBackfillRegistry()
    reg.start(writes_enabled=False)

    def boom() -> Any:
        raise RuntimeError("kaboom")

    sweep(reg, edit_lib, delay=0.0, write=False, make_plugin=boom)
    s = reg.state()
    assert s.phase == "failed"
    assert "kaboom" in (s.error or "")


def test_lyrics_coverage(edit_lib: Library) -> None:
    from app.beets.lyrics import lyrics_coverage

    item = sorted(next(iter(edit_lib.albums())).items(), key=lambda it: it.track)[0]
    item.lyrics = "x"
    item.store()
    cov = lyrics_coverage(edit_lib)
    assert cov.total == 3
    assert cov.with_lyrics == 1
    assert cov.percent == pytest.approx(33.3, abs=0.1)


def test_per_album_fetch_409_during_backfill(
    edit_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running backfill blocks the per-album fetch op (409)."""
    import asyncio

    from fastapi import HTTPException

    from app.beets.lyrics import fetch_album_lyrics_op
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill().start(writes_enabled=True)

    class _App:
        class state:
            beets_library = None

    class _Req:
        app = _App()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(fetch_album_lyrics_op(_Req(), 1))
    assert ei.value.status_code == 409
    reset_lyrics_backfill()


def test_backfill_active_helper_reflects_state() -> None:
    from app.lyrics_jobs.registry import lyrics_backfill_active, reset_lyrics_backfill

    reg = reset_lyrics_backfill()
    assert lyrics_backfill_active() is False
    reg.start(writes_enabled=True)
    assert lyrics_backfill_active() is True
    reset_lyrics_backfill()


# The following pair proves the autouse ``reset_lyrics_backfill_registry``
# fixture in conftest cleans the GLOBAL backfill registry between tests: the
# first leaves a backfill running with NO manual cleanup; the second (which
# runs after it) must still see an idle slot. Without the autouse reset the
# leaked "running" job would make the second assertion fail.
def test_leaks_a_running_backfill_for_the_next_test() -> None:
    from app.lyrics_jobs.registry import get_lyrics_backfill, lyrics_backfill_active

    get_lyrics_backfill().start(writes_enabled=True)
    assert lyrics_backfill_active() is True  # deliberately left running


def test_global_backfill_registry_is_idle_at_test_entry() -> None:
    from app.lyrics_jobs.registry import lyrics_backfill_active

    assert lyrics_backfill_active() is False


def test_registry_album_scope_in_state() -> None:
    from app.lyrics_jobs.registry import LyricsBackfillRegistry

    reg = LyricsBackfillRegistry()
    # library scope defaults
    reg.start(writes_enabled=True)
    s = reg.state()
    assert s.album_id is None and s.scope_label == "library"
    reg.finish("done")

    # album scope
    reg.start(writes_enabled=True, album_id=42, scope_label="Adele — 25")
    s = reg.state()
    assert s.album_id == 42 and s.scope_label == "Adele — 25"


def test_sweep_album_scope_only_touches_that_album(edit_lib: Library) -> None:
    from beets.library import Item

    from app.lyrics_jobs.registry import LyricsBackfillRegistry
    from app.lyrics_jobs.runner import sweep

    # edit_lib has one album (Radiohead, 3 tracks). Add a second 1-track album.
    extra = Item(
        album="Other", albumartist="Someone", artist="Someone", title="Solo", track=1, disc=1
    )
    edit_lib.add_album([extra])
    target_id = int(next(iter(edit_lib.albums())).id)  # Radiohead album (first added)

    reg = LyricsBackfillRegistry()
    reg.start(writes_enabled=False, album_id=target_id, scope_label="Radiohead — In Rainbows")

    def fake_fetch_one(
        plugin: object, item: object, *, force: bool, write: bool
    ) -> ItemLyricsOutcome:
        return _outcome("found")

    sweep(
        reg,
        edit_lib,
        delay=0.0,
        write=False,
        album_id=target_id,
        fetch_one=fake_fetch_one,
        make_plugin=lambda: object(),
    )
    s = reg.state()
    assert s.phase == "done"
    assert s.total == 3 and s.processed == 3  # only the Radiohead album, not the 4th item
