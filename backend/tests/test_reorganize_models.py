from app.models.reorganize import (
    ReorganizeBackfillStatus,
    ReorganizeCollision,
    ReorganizeConflict,
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
        will_move=41,
        already_in_place=8,
        moves=[],
        truncated=True,
        orphans=[],
        orphans_total=0,
        conflicts=[],
        conflicts_total=1,
    )
    assert p.scope == "library"
    assert p.already_in_place == 8
    assert p.truncated is True
    # The three buckets partition the scope: a conflicted unit is neither a move
    # nor "already in place".
    assert p.will_move + p.already_in_place + p.conflicts_total == p.total


def test_conflict_carries_its_collisions() -> None:
    c = ReorganizeConflict(
        kind="album",
        label="X - Collide",
        from_path="/m/junk/dup",
        collisions=[
            ReorganizeCollision(
                kind="cross_unit",
                path="X/Collide/01 Song.mp3",
                detail="X/Collide/01 Song.mp3: already exists on disk and holds track 1 'Song'",
            )
        ],
    )
    assert c.kind == "album"
    assert c.collisions[0].kind == "cross_unit"


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
        playlists_reexported=0,
        failures=[],
        finished_at=None,
    )
    assert s.phase == "idle"
    assert s.artist is None
    assert s.album_id is None
    assert s.finished_at is None  # nothing has finished, so nothing is dated
