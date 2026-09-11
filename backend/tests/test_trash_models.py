from app.models.trash import (
    EmptyResult,
    RestoreRequest,
    RestoreResult,
    TrashedAlbum,
    TrashListing,
)


def test_trashed_album_minimal() -> None:
    a = TrashedAlbum(
        folder="Artist - Album",
        album_artist=None,
        album=None,
        year=None,
        track_count=0,
        format=None,
        restore_mode="import",
        restore_note="no record of where this came from",
        origin=None,
    )
    assert a.folder == "Artist - Album"
    assert a.restore_mode == "import"


def test_the_moved_aside_mode_is_on_the_wire() -> None:
    """``by_hand`` is a contract value, so the generated client has a branch for
    it: the UI renders no Restore button on this row."""
    a = TrashedAlbum(
        folder="ABBA - artist image",
        album_artist=None,
        album=None,
        year=None,
        track_count=0,
        format=None,
        restore_mode="by_hand",
        restore_note="MusicDrop replaced these files; they are not an album.",
        origin="/data/cache/artist-images",
    )
    assert a.restore_mode == "by_hand"
    assert a.origin == "/data/cache/artist-images"


def test_listing_and_results() -> None:
    listing = TrashListing(
        albums=[
            TrashedAlbum(
                folder="x",
                album_artist="A",
                album="B",
                year=1994,
                track_count=3,
                format="FLAC",
                restore_mode="move_back",
                restore_note=None,
                origin="/music/A/B",
            )
        ],
        trash_path="/data/beets/trash",
    )
    assert listing.albums[0].track_count == 3
    assert RestoreRequest(folder="x").folder == "x"
    assert RestoreResult(restored=True, reason="restored", album_id=7).album_id == 7
    assert RestoreResult(restored=False, reason="already_in_library").album_id is None
    assert RestoreResult(restored=False, reason="origin_occupied").restored is False
    assert listing.albums[0].origin == "/music/A/B"
    assert EmptyResult(removed=2).removed == 2
