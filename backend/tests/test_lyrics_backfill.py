from __future__ import annotations


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
