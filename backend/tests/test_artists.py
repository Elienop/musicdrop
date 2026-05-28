import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.beets.library import list_artists
from app.main import app
from app.models.artist import Artist
from tests.conftest import make_test_handle


def _make_item(directory: Path, *, album: str, albumartist: str, title: str, track: int) -> Item:
    item = Item(album=album, albumartist=albumartist, title=title, track=track)
    item.path = os.fsencode(str(directory / f"{albumartist} - {title}.mp3"))
    return item


def _add_album(lib: Library, directory: Path, *, album: str, albumartist: str) -> None:
    item = _make_item(directory, album=album, albumartist=albumartist, title=album, track=1)
    lib.add_album([item])


@pytest.fixture
def temp_library(tmp_path: Path) -> Library:
    """Hermetic library: ABBA has two albums, a-ha has one."""
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))
    _add_album(lib, tmp_path, album="Arrival", albumartist="ABBA")
    _add_album(lib, tmp_path, album="Voulez-Vous", albumartist="ABBA")
    _add_album(lib, tmp_path, album="Hunting High and Low", albumartist="a-ha")
    return lib


@pytest.fixture
def client(temp_library: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(temp_library, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_artists_roster_groups_counts_and_sorts(client: TestClient) -> None:
    resp = client.get("/api/artists")
    assert resp.status_code == 200
    body = resp.json()
    # Sorted by name casefold: "a-ha" before "ABBA".
    assert body == [
        {"name": "a-ha", "album_count": 1},
        {"name": "ABBA", "album_count": 2},
    ]


def test_artists_roster_excludes_empty_album_artist(tmp_path: Path) -> None:
    # A whitespace-only albumartist must be dropped: _coerce_str does not strip,
    # so only the skip's own .strip() keeps this blank card out of the roster.
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))
    _add_album(lib, tmp_path, album="Arrival", albumartist="ABBA")
    _add_album(lib, tmp_path, album="Mystery", albumartist="   ")

    artists = list_artists(lib)

    assert artists == [Artist(name="ABBA", album_count=1)]
    assert all(a.name.strip() for a in artists)
