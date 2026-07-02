from app.models.reorganize import (
    ReorganizeBackfillStatus,
    ReorganizeMove,
    ReorganizeOutcome,
    ReorganizePlan,
)


def test_move_holds_album_dirs() -> None:
    m = ReorganizeMove(
        kind="album",
        label="Radiohead — In Rainbows",
        from_path="/m/Old",
        to_path="/m/Radiohead/In Rainbows",
        track_count=10,
    )
    assert m.kind == "album"
    assert m.track_count == 10


def test_plan_counts_and_truncation() -> None:
    p = ReorganizePlan(
        scope="library",
        scope_label="library",
        total=50,
        will_move=42,
        already_in_place=8,
        moves=[],
        truncated=True,
        orphans=[],
        orphans_total=0,
    )
    assert p.scope == "library"
    assert p.already_in_place == 8
    assert p.truncated is True


def test_outcome_defaults_error_none() -> None:
    o = ReorganizeOutcome(status="moved", label="X — Y")
    assert o.error is None


def test_status_idle_shape() -> None:
    s = ReorganizeBackfillStatus(
        phase="idle",
        job_id=None,
        scope=None,
        total=0,
        processed=0,
        moved=0,
        skipped=0,
        failed=0,
        current=None,
        error=None,
        artist=None,
        album_id=None,
        scope_label="library",
        orphans_trashed=0,
    )
    assert s.phase == "idle"
    assert s.artist is None and s.album_id is None
