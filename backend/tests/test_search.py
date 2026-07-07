import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from beets.library import Item, Library
from fastapi.concurrency import run_in_threadpool as _real_run_in_threadpool
from fastapi.testclient import TestClient

import app.api.search as search_mod
from app.api.albums import get_library
from app.beets.library import search, search_typed
from app.main import app
from app.models.search import SearchResults
from tests.conftest import make_test_handle


def _threadpool_spy(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Record every callable offloaded via the router's run_in_threadpool while
    still executing it (real passthrough)."""
    spy = Mock(side_effect=lambda fn, *a, **k: _real_run_in_threadpool(fn, *a, **k))
    monkeypatch.setattr(search_mod, "run_in_threadpool", spy, raising=False)
    return spy


def _offloaded_callables(spy: Mock) -> list[object]:
    return [call.args[0] for call in spy.call_args_list]


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
def client(temp_library: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(temp_library, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
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


def test_typed_search_tracks_pages_with_total(client: TestClient) -> None:
    # All three track titles match "o" (Love Me Do, Waterloo, Take On Me);
    # limit=2 pages them and `total` reports the full match count.
    page1_resp = client.get(
        "/api/search", params={"q": "o", "type": "tracks", "limit": 2, "offset": 0}
    )
    assert page1_resp.status_code == 200
    page1 = page1_resp.json()
    assert page1["type"] == "tracks"
    assert page1["total"] == 3
    assert page1["limit"] == 2
    assert page1["offset"] == 0
    assert len(page1["tracks"]) == 2
    assert page1["artists"] == []
    assert page1["albums"] == []

    page2 = client.get(
        "/api/search", params={"q": "o", "type": "tracks", "limit": 2, "offset": 2}
    ).json()
    assert page2["total"] == 3
    assert len(page2["tracks"]) == 1
    titles1 = {t["title"] for t in page1["tracks"]}
    titles2 = {t["title"] for t in page2["tracks"]}
    # Stable ordering across requests: the pages are disjoint and exhaustive.
    assert titles1.isdisjoint(titles2)
    assert titles1 | titles2 == {"Love Me Do", "Waterloo", "Take On Me"}


def test_typed_search_albums_returns_only_albums(client: TestClient) -> None:
    resp = client.get("/api/search", params={"q": "love", "type": "albums"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "albums"
    assert [a["title"] for a in body["albums"]] == ["Lovesong"]
    assert body["total"] == 1
    assert body["artists"] == []
    assert body["tracks"] == []
    assert body["limit"] == 25  # defaults echoed
    assert body["offset"] == 0


def test_typed_search_artists_pages_the_roster(client: TestClient) -> None:
    # Roster order is casefolded name: "a-ha" < "ABBA" ("-" sorts before "b");
    # "Love" contains no "a". limit=1 offset=1 must return ONLY the second match.
    resp = client.get("/api/search", params={"q": "a", "type": "artists", "limit": 1, "offset": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "artists"
    assert body["total"] == 2
    assert [a["name"] for a in body["artists"]] == ["ABBA"]


def test_typed_search_blank_query_returns_empty_page(client: TestClient) -> None:
    resp = client.get(
        "/api/search", params={"q": "   ", "type": "tracks", "limit": 48, "offset": 96}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "type": "tracks",
        "artists": [],
        "albums": [],
        "tracks": [],
        "total": 0,
        "limit": 48,
        "offset": 96,
    }


def test_typed_search_malformed_query_does_not_500(client: TestClient) -> None:
    # Same ParsingError guard as the sectioned mode: an unbalanced quote
    # degrades to zero results, never a 500.
    resp = client.get("/api/search", params={"q": '"', "type": "albums"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_typed_search_invalid_type_is_422(client: TestClient) -> None:
    resp = client.get("/api/search", params={"q": "love", "type": "bogus"})
    assert resp.status_code == 422


def test_sectioned_search_offloads_scan_to_threadpool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The library scan must not run on the event loop — it offloads like browse.py.
    spy = _threadpool_spy(monkeypatch)
    resp = client.get("/api/search", params={"q": "love"})
    assert resp.status_code == 200
    assert search in _offloaded_callables(spy)


def test_typed_search_offloads_scan_to_threadpool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _threadpool_spy(monkeypatch)
    resp = client.get("/api/search", params={"q": "love", "type": "albums"})
    assert resp.status_code == 200
    assert search_typed in _offloaded_callables(spy)


def test_default_mode_shape_is_unchanged(client: TestClient) -> None:
    # The Phase-3 pin: no `type` => today's sectioned response, byte-identical.
    base = client.get("/api/search", params={"q": "love"})
    assert base.status_code == 200
    assert set(base.json().keys()) == {
        "artists",
        "albums",
        "tracks",
        "artist_total",
        "album_total",
        "track_total",
    }
    # `offset` without `type` is accepted-but-ignored (sectioned mode is not
    # paged): the response bytes are identical.
    with_offset = client.get("/api/search", params={"q": "love", "offset": 10})
    assert with_offset.content == base.content
