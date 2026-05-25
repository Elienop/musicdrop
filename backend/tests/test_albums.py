import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.config import settings
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


# A 1x1 transparent PNG — smallest valid PNG payload.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f0a0000000049454e44ae42"
    "6082"
)


def test_album_cover_from_artpath(temp_library: Library, tmp_path: Path) -> None:
    # Point one album's artpath at a real PNG on disk and assert it serves.
    art_file = tmp_path / "cover.png"
    art_file.write_bytes(_TINY_PNG)

    albums = sorted(temp_library.albums(), key=lambda a: int(a.id))
    album = albums[0]
    album["artpath"] = os.fsencode(str(art_file))
    album.store()

    app.dependency_overrides[get_library] = lambda: temp_library
    try:
        resp = TestClient(app).get(f"/api/albums/{album.id}/cover")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert resp.content == _TINY_PNG


def test_album_cover_without_art_returns_404(client: TestClient, temp_library: Library) -> None:
    # Albums in the fixture have no artpath and no real files with embedded art.
    album = next(iter(temp_library.albums()))
    resp = client.get(f"/api/albums/{album.id}/cover")
    assert resp.status_code == 404


def test_abs_path_resolves_relative_against_directory(temp_library: Library) -> None:
    # beets stores file paths relative to lib.directory; _abs_path must join a
    # relative stored path with directory and leave an absolute path untouched.
    from app.beets.library import _abs_path

    directory = os.fsdecode(temp_library.directory)
    relative = _abs_path(temp_library, os.fsencode(os.path.join("Artist", "Album", "t.flac")))
    assert relative == os.path.join(directory, "Artist", "Album", "t.flac")

    already_abs = os.path.join(directory, "x.flac")
    assert _abs_path(temp_library, os.fsencode(already_abs)) == already_abs


def test_album_cover_missing_album_returns_404(client: TestClient) -> None:
    resp = client.get("/api/albums/999999/cover")
    assert resp.status_code == 404


def test_album_cover_unconfigured_library_returns_404() -> None:
    app.dependency_overrides[get_library] = lambda: None
    try:
        resp = TestClient(app).get("/api/albums/1/cover")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_lifespan_opens_library_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build a real library on disk, point settings at it, and confirm the
    # startup lifespan opens it onto app.state so get_library serves it WITHOUT
    # any dependency override or per-request open.
    db_path = tmp_path / "library.db"
    lib = Library(str(db_path), directory=str(tmp_path))
    lib.add_album(
        [
            _make_item(
                tmp_path,
                album="Arrival",
                albumartist="ABBA",
                year=1976,
                genre="Pop",
                title="SOS",
                track=1,
            )
        ]
    )
    lib._close()

    monkeypatch.setattr(settings, "beets_library_path", str(db_path))
    monkeypatch.setattr(settings, "beets_library_directory", str(tmp_path))

    app.dependency_overrides.clear()
    with TestClient(app) as client:  # context-manager form runs the lifespan
        assert app.state.beets_library is not None
        resp = client.get("/api/albums")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["album_artist"] == "ABBA"
