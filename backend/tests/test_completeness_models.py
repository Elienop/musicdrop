from app.models.completeness import AlbumMissingReport, MissingReleaseTrack


def test_missing_release_track_shape() -> None:
    t = MissingReleaseTrack(index=3, disc=1, title="Nude", duration_seconds=255.0, mb_trackid="t3")
    assert t.index == 3
    assert t.disc == 1
    assert t.mb_trackid == "t3"


def test_album_missing_report_defaults_and_status() -> None:
    report = AlbumMissingReport(
        status="ok",
        total=12,
        present_count=9,
        missing=[
            MissingReleaseTrack(index=10, disc=1, title="x", duration_seconds=None, mb_trackid=None)
        ],
        source="MusicBrainz",
    )
    assert report.status == "ok"
    assert report.total == 12
    assert report.present_count == 9
    assert len(report.missing) == 1


def test_album_missing_report_rejects_unknown_status() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AlbumMissingReport(status="bogus", total=0, present_count=0, missing=[], source=None)  # type: ignore[arg-type]  # deliberately invalid status to test Pydantic rejection
