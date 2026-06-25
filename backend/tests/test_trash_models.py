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
    )
    assert a.folder == "Artist - Album"


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
            )
        ],
        trash_path="/data/beets/trash",
    )
    assert listing.albums[0].track_count == 3
    assert RestoreRequest(folder="x").folder == "x"
    assert RestoreResult(restored=True, reason="restored", album_id=7).album_id == 7
    assert RestoreResult(restored=False, reason="already_in_library").album_id is None
    assert EmptyResult(removed=2).removed == 2
