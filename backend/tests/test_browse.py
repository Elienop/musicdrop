"""Faceted Browse — adapter (facets + filtered albums) and the two endpoints.

One representative (genre, decade, format) per album, derived from the album +
its items, so facet counts sum to the album total and AND/OR filtering is
unambiguous. Hermetic temp library; no real files, no network.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.beets.library import browse_albums, browse_facets
from app.main import app
from tests.conftest import make_test_handle


def _add(
    lib: Library,
    directory: Path,
    *,
    artist: str,
    album: str,
    year: int | None,
    genre: str | None,
    fmt: str | None,
    tracks: int = 2,
) -> None:
    items = []
    for i in range(1, tracks + 1):
        it = Item(album=album, albumartist=artist, artist=artist, title=f"T{i}", track=i)
        it.path = os.fsencode(str(directory / f"{artist} - {album} - {i}.x"))
        if genre is not None:
            it.genre = genre
        if fmt is not None:
            it.format = fmt
        items.append(it)
    al = lib.add_album(items)
    if genre is not None:
        al["genre"] = genre
    al.year = year or 0
    al.store()


@pytest.fixture
def browse_lib(tmp_path: Path) -> Library:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    _add(lib, tmp_path, artist="ABBA", album="Arrival", year=1976, genre="Pop", fmt="FLAC")
    _add(lib, tmp_path, artist="AC/DC", album="Back in Black", year=1980, genre="Rock", fmt="MP3")
    _add(lib, tmp_path, artist="Adele", album="25", year=2015, genre="Pop", fmt="FLAC")
    _add(
        lib,
        tmp_path,
        artist="Metallica",
        album="Ride the Lightning",
        year=2015,
        genre="Metal",
        fmt="FLAC",
    )
    _add(lib, tmp_path, artist="Mystery", album="Untitled", year=None, genre="Rock", fmt="MP3")
    _add(lib, tmp_path, artist="Quiet", album="No Format", year=2015, genre="Jazz", fmt=None)
    return lib


@pytest.fixture
def client(browse_lib: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(browse_lib, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_library, None)


# ----- adapter: facets -----


def test_facets_counts(browse_lib: Library) -> None:
    facets = browse_facets(browse_lib)
    genres = {f.value: f.count for f in facets.genres}
    decades = {f.value: f.count for f in facets.decades}
    formats = {f.value: f.count for f in facets.formats}
    assert genres == {"Pop": 2, "Rock": 2, "Metal": 1, "Jazz": 1}
    assert decades == {"2010s": 3, "1970s": 1, "1980s": 1, "Unknown": 1}
    assert formats == {"FLAC": 3, "MP3": 2, "Unknown": 1}
    # Genre/format counts sum to the album total (one value per album).
    assert sum(genres.values()) == 6
    assert sum(formats.values()) == 6


def test_facets_decades_sorted_newest_first_unknown_last(browse_lib: Library) -> None:
    order = [f.value for f in browse_facets(browse_lib).decades]
    assert order == ["2010s", "1980s", "1970s", "Unknown"]


# ----- adapter: filtered albums -----


def _names(albums: list) -> set[str]:  # type: ignore[type-arg]  # test helper over Album models
    return {a.title for a in albums}


def test_browse_no_filters_returns_all(browse_lib: Library) -> None:
    albums, total = browse_albums(browse_lib, genres=[], decades=[], formats=[], limit=50, offset=0)
    assert total == 6
    assert len(albums) == 6


def test_browse_single_genre(browse_lib: Library) -> None:
    albums, total = browse_albums(
        browse_lib, genres=["Pop"], decades=[], formats=[], limit=50, offset=0
    )
    assert total == 2
    assert _names(albums) == {"Arrival", "25"}


def test_browse_or_within_facet(browse_lib: Library) -> None:
    _albums, total = browse_albums(
        browse_lib, genres=["Pop", "Metal"], decades=[], formats=[], limit=50, offset=0
    )
    assert total == 3  # union: Pop (2) + Metal (1)


def test_browse_and_across_facets(browse_lib: Library) -> None:
    albums, total = browse_albums(
        browse_lib, genres=["Pop"], decades=["2010s"], formats=[], limit=50, offset=0
    )
    assert total == 1
    assert _names(albums) == {"25"}  # only Adele/25 is Pop AND 2010s


def test_browse_format_filter(browse_lib: Library) -> None:
    _albums, total = browse_albums(
        browse_lib, genres=[], decades=[], formats=["FLAC"], limit=50, offset=0
    )
    assert total == 3


def test_browse_no_match_is_empty(browse_lib: Library) -> None:
    albums, total = browse_albums(
        browse_lib, genres=["Disco"], decades=[], formats=[], limit=50, offset=0
    )
    assert total == 0
    assert albums == []


def test_browse_pagination(browse_lib: Library) -> None:
    page1, total = browse_albums(browse_lib, genres=[], decades=[], formats=[], limit=2, offset=0)
    page2, _ = browse_albums(browse_lib, genres=[], decades=[], formats=[], limit=2, offset=2)
    assert total == 6
    assert len(page1) == 2
    assert _names(page1).isdisjoint(_names(page2))  # stable, non-overlapping pages


# ----- API -----


def test_facets_endpoint(client: TestClient) -> None:
    resp = client.get("/api/browse/facets")
    assert resp.status_code == 200
    body = resp.json()
    assert {f["value"] for f in body["genres"]} == {"Pop", "Rock", "Metal", "Jazz"}


def test_browse_albums_endpoint_filters(client: TestClient) -> None:
    resp = client.get("/api/browse/albums", params={"genre": "Pop", "decade": "2010s"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "25"


def test_browse_albums_endpoint_repeated_params(client: TestClient) -> None:
    # ?genre=Pop&genre=Metal -> OR within the genre facet.
    resp = client.get("/api/browse/albums?genre=Pop&genre=Metal")
    assert resp.status_code == 200
    assert resp.json()["total"] == 3


def test_browse_albums_endpoint_bad_limit(client: TestClient) -> None:
    assert client.get("/api/browse/albums", params={"limit": 0}).status_code == 422
