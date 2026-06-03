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
