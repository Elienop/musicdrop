from app.models.album import Album
from app.models.stats import LibraryStats, LibraryStatsResponse


def test_library_stats_fields() -> None:
    s = LibraryStats(
        track_count=10,
        album_count=3,
        artist_count=2,
        total_seconds=1234.5,
        total_bytes=999,
    )
    assert s.track_count == 10
    assert s.total_seconds == 1234.5


def test_response_defaults_size_is_estimate_true() -> None:
    album = Album(
        id=1,
        album_artist="Adele",
        title="25",
        year=2015,
        track_count=11,
        genre="Pop",
        mb_albumid=None,
    )
    resp = LibraryStatsResponse(
        stats=LibraryStats(
            track_count=11,
            album_count=1,
            artist_count=1,
            total_seconds=2600.0,
            total_bytes=123,
        ),
        recently_added=[album],
    )
    assert resp.size_is_estimate is True
    assert resp.recently_added[0].title == "25"
