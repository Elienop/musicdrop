from __future__ import annotations

import pytest

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
