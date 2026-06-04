from app.artist_art_jobs.registry import (
    ArtistArtBackfillRegistry,
    artist_art_backfill_active,
    reset_artist_art_backfill,
)
from app.models.artist_art import ArtistArtOutcome, ArtistArtStatus


def _out(status: ArtistArtStatus) -> ArtistArtOutcome:
    return ArtistArtOutcome(
        artist="A", status=status, written=1 if status == "written" else 0, dirs=1
    )


def test_single_slot_and_counters() -> None:
    reg = ArtistArtBackfillRegistry()
    reg.start(force=True, artist="A", scope_label="A")
    assert reg.is_running()
    try:
        reg.start(force=True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("second start should raise")
    reg.record(_out("written"))
    reg.record(_out("skipped"))
    reg.record(_out("failed"))
    st = reg.state()
    assert (st.processed, st.written, st.skipped, st.failed) == (3, 1, 1, 1)
    reg.finish("done")
    assert reg.state().phase == "done"


def test_module_active_flag_and_reset() -> None:
    reg = reset_artist_art_backfill()
    assert artist_art_backfill_active() is False
    reg.start(force=False)
    assert artist_art_backfill_active() is True
    reset_artist_art_backfill()
    assert artist_art_backfill_active() is False
