import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.beets.library import search
from app.main import app
from app.models.search import SearchResults


def _make_item(directory: Path, *, album: str, albumartist: str, title: str, track: int) -> Item:
    # Set both artist and albumartist: SearchTrack reports the track-level
    # artist, so the fixture mirrors a real tagged file where they match.
    item = Item(album=album, albumartist=albumartist, artist=albumartist, title=title, track=track)
    item.path = os.fsencode(str(directory / f"{albumartist} - {title}.mp3"))
    return item


def _add_album(lib: Library, directory: Path, *, album: str, albumartist: str, title: str) -> None:
    item = _make_item(directory, album=album, albumartist=albumartist, title=title, track=1)
    lib.add_album([item])


@pytest.fixture
def temp_library(tmp_path: Path) -> Library:
    """Hermetic library spanning three artists.

    "love" is engineered to hit exactly one of each type: the artist "Love",
    the album "Lovesong" (no other album title/artist contains the substring),
    and the track "Love Me Do". Note beets free-text also matches albumartist,
    so the album list for "love" includes every album by the artist Love.
    """
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path))
    _add_album(lib, tmp_path, album="Lovesong", albumartist="Love", title="Love Me Do")
    _add_album(lib, tmp_path, album="Arrival", albumartist="ABBA", title="Waterloo")
    _add_album(lib, tmp_path, album="Hunting High", albumartist="a-ha", title="Take On Me")
    return lib


@pytest.fixture
def client(temp_library: Library) -> Iterator[TestClient]:
    app.dependency_overrides[get_library] = lambda: temp_library
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_search_matches_artist_album_and_track(client: TestClient) -> None:
    resp = client.get("/api/search", params={"q": "love"})
    assert resp.status_code == 200
    body = resp.json()

    assert [a["name"] for a in body["artists"]] == ["Love"]
    assert body["artist_total"] == 1

    assert [a["title"] for a in body["albums"]] == ["Lovesong"]
    assert body["album_total"] == 1

    assert [t["title"] for t in body["tracks"]] == ["Love Me Do"]
    assert body["track_total"] == 1


def test_search_track_carries_album_id(client: TestClient) -> None:
    resp = client.get("/api/search", params={"q": "Love Me Do"})
    body = resp.json()
    track = body["tracks"][0]
    assert track["title"] == "Love Me Do"
    assert track["artist"] == "Love"
    assert track["album"] == "Lovesong"
    assert isinstance(track["album_id"], int)
    assert track["album_id"] > 0


def test_search_totals_reflect_full_count_while_lists_respect_limit(
    temp_library: Library,
) -> None:
    # All three track titles share the substring "o" (Love Me Do, Waterloo,
    # Take On Me); limit=2 caps the list but the total must report the full
    # match count for "N of M".
    results = search(temp_library, query="o", limit=2)
    assert results.track_total == 3
    assert len(results.tracks) == 2


def test_search_blank_query_returns_empty(client: TestClient) -> None:
    resp = client.get("/api/search", params={"q": "   "})
    assert resp.status_code == 200
    assert resp.json() == {
        "artists": [],
        "albums": [],
        "tracks": [],
        "artist_total": 0,
        "album_total": 0,
        "track_total": 0,
    }


def test_search_missing_query_param_returns_empty(client: TestClient) -> None:
    resp = client.get("/api/search")
    assert resp.status_code == 200
    assert resp.json()["track_total"] == 0


def test_search_no_match_returns_empty(client: TestClient) -> None:
    resp = client.get("/api/search", params={"q": "zzzznomatchterm"})
    body = resp.json()
    assert body["artists"] == []
    assert body["albums"] == []
    assert body["tracks"] == []
    assert body["artist_total"] == 0
    assert body["album_total"] == 0
    assert body["track_total"] == 0


def test_search_unconfigured_library_returns_empty() -> None:
    app.dependency_overrides[get_library] = lambda: None
    try:
        resp = TestClient(app).get("/api/search", params={"q": "love"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["track_total"] == 0


def test_search_malformed_query_does_not_500(client: TestClient) -> None:
    # An unbalanced quote makes beets' parser raise InvalidQueryError; the
    # adapter must swallow it and treat that entity as no results.
    resp = client.get("/api/search", params={"q": '"'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tracks"] == []
    assert body["albums"] == []
    assert body["track_total"] == 0
    assert body["album_total"] == 0


def test_search_returns_search_results_model(temp_library: Library) -> None:
    results = search(temp_library, query="love", limit=25)
    assert isinstance(results, SearchResults)
