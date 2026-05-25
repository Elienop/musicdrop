import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app


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
def client(temp_library: Library) -> Iterator[TestClient]:
    app.dependency_overrides[get_library] = lambda: temp_library
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


def test_artists_unconfigured_library_returns_empty_roster() -> None:
    app.dependency_overrides[get_library] = lambda: None
    try:
        resp = TestClient(app).get("/api/artists")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == []
