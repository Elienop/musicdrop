import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.main import app


def _make_item(
    directory: Path, *, album: str, albumartist: str, year: int, genre: str, title: str, track: int
) -> Item:
    item = Item(
        album=album,
        albumartist=albumartist,
        year=year,
        genre=genre,
        title=title,
        track=track,
    )
    item.path = os.fsencode(str(directory / f"{albumartist} - {title}.mp3"))
    return item


@pytest.fixture
def temp_library(tmp_path: Path) -> Library:
    """Build a tiny hermetic beets library with two albums (no real files, no network)."""
    db_path = tmp_path / "library.db"
    lib = Library(str(db_path), directory=str(tmp_path))

    # Album A: ABBA / Arrival (1976, Pop) — two tracks
    arrival = [
        _make_item(
            tmp_path,
            album="Arrival",
            albumartist="ABBA",
            year=1976,
            genre="Pop",
            title="When I Kissed the Teacher",
            track=1,
        ),
        _make_item(
            tmp_path,
            album="Arrival",
            albumartist="ABBA",
            year=1976,
            genre="Pop",
            title="Dancing Queen",
            track=2,
        ),
    ]
    album_a = lib.add_album(arrival)
    album_a["genre"] = "Pop"
    album_a.store()

    # Album B: a-ha / Hunting High and Low (1985, Synthpop) — one track
    hunting = [
        _make_item(
            tmp_path,
            album="Hunting High and Low",
            albumartist="a-ha",
            year=1985,
            genre="Synthpop",
            title="Take On Me",
            track=1,
        ),
    ]
    album_b = lib.add_album(hunting)
    album_b["genre"] = "Synthpop"
    album_b.store()

    return lib


@pytest.fixture
def client(temp_library: Library) -> Iterator[TestClient]:
    app.dependency_overrides[get_library] = lambda: temp_library
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_albums_returns_mapped_models(client: TestClient) -> None:
    resp = client.get("/api/albums")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    # Stable case-insensitive sort by albumartist then album. Under codepoint
    # ordering "a-ha" precedes "abba" (the '-' sorts before 'b'), so a-ha is first.
    first = body["items"][0]
    assert first["album_artist"] == "a-ha"
    assert first["title"] == "Hunting High and Low"
    assert first["year"] == 1985
    assert first["track_count"] == 1
    assert first["genre"] == "Synthpop"

    second = body["items"][1]
    assert second["album_artist"] == "ABBA"
    assert second["title"] == "Arrival"
    assert second["year"] == 1976
    assert second["track_count"] == 2
    assert second["genre"] == "Pop"


def test_pagination_limit_and_offset(client: TestClient) -> None:
    page1 = client.get("/api/albums?limit=1&offset=0").json()
    assert page1["total"] == 2
    assert page1["limit"] == 1
    assert page1["offset"] == 0
    assert len(page1["items"]) == 1
    assert page1["items"][0]["album_artist"] == "a-ha"

    page2 = client.get("/api/albums?limit=1&offset=1").json()
    assert page2["total"] == 2
    assert page2["offset"] == 1
    assert len(page2["items"]) == 1
    assert page2["items"][0]["album_artist"] == "ABBA"


def test_limit_is_capped_at_200(client: TestClient) -> None:
    resp = client.get("/api/albums?limit=9999")
    assert resp.status_code == 422


def test_missing_library_returns_empty_page() -> None:
    # When no library can be resolved (unset/missing path), get_library yields
    # None and the endpoint degrades to an empty page instead of crashing.
    app.dependency_overrides[get_library] = lambda: None
    try:
        resp = TestClient(app).get("/api/albums")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []
