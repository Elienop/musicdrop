"""Bank duplicate detection — the adapter + the lazy endpoint.

The adapter (`find_import_duplicates`) calls beets' OWN `Album.duplicates_query`
with the configured `import.duplicate_keys.album` (default `albumartist album`)
on the matched-release metadata, so its prediction equals what the apply does.
The endpoint surfaces it for a banked candidate row.
"""

from __future__ import annotations

from beets.library import Item, Library

from app.beets.duplicates import find_import_duplicates
from app.beets.library import LibraryHandle


def _add_album(lib: Library, *, artist: str, album: str, mb: str | None, n: int) -> int:
    items = [
        Item(album=album, albumartist=artist, title=f"t{i}", mb_albumid=mb or "") for i in range(n)
    ]
    al = lib.add_album(items)  # adds items too
    al.store()
    return int(al.id)


# ----- adapter (uses the beets_library fixture directly) -----


def test_detects_existing_album_by_default_keys(beets_library: LibraryHandle) -> None:
    lib = beets_library.lib
    aid = _add_album(lib, artist="2Pac", album="Me Against the World", mb="mb-x", n=15)
    found = find_import_duplicates(
        lib, albumartist="2Pac", album="Me Against the World", year=1995, mb_albumid="mb-x"
    )
    assert [e.album_id for e in found] == [aid]
    assert found[0].album_artist == "2Pac"


def test_no_collision_returns_empty(beets_library: LibraryHandle) -> None:
    lib = beets_library.lib
    _add_album(lib, artist="2Pac", album="Me Against the World", mb="mb-x", n=15)
    assert (
        find_import_duplicates(lib, albumartist="Nas", album="Illmatic", year=1994, mb_albumid=None)
        == []
    )


def test_no_albumartist_skips_like_beets(beets_library: LibraryHandle) -> None:
    # beets find_duplicates returns [] when artist is None (tasks.py:398-400).
    assert (
        find_import_duplicates(
            beets_library.lib, albumartist=None, album="x", year=None, mb_albumid=None
        )
        == []
    )
