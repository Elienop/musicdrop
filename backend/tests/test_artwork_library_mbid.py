from beets.library import Library

from app.beets.library import get_artist_mbid

VALID = "cc2c9c3c-b7bc-4b8b-84d8-4fbd8779e493"


def test_returns_valid_uuid(edit_lib: Library) -> None:
    album = next(iter(edit_lib.albums()))
    album.albumartist = "MBID Artist"
    album.mb_albumartistid = VALID
    album.store()
    assert get_artist_mbid(edit_lib, "MBID Artist") == VALID


def test_rejects_junk_mbid(edit_lib: Library) -> None:
    album = next(iter(edit_lib.albums()))
    album.albumartist = "Junk Artist"
    album.mb_albumartistid = "520"
    album.store()
    assert get_artist_mbid(edit_lib, "Junk Artist") is None


def test_none_when_artist_absent(edit_lib: Library) -> None:
    assert get_artist_mbid(edit_lib, "No Such Artist 12345") is None


def test_name_with_quote_does_not_break_query(edit_lib: Library) -> None:
    album = next(iter(edit_lib.albums()))
    album.albumartist = 'A "Quoted" Name'
    album.mb_albumartistid = VALID
    album.store()
    # Would break a string-interpolated `albumartist:"..."` query.
    assert get_artist_mbid(edit_lib, 'A "Quoted" Name') == VALID
