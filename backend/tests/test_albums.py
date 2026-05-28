import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.config import settings
from app.main import app
from tests.conftest import make_test_handle


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
def client(temp_library: Library, tmp_path: Path) -> Iterator[TestClient]:
    handle = make_test_handle(temp_library, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
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

    handle = make_test_handle(temp_library, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
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


def test_album_detail_returns_album_with_sorted_tracklist(
    client: TestClient, temp_library: Library
) -> None:
    # Build a fresh album whose items are added deliberately out of (disc, track)
    # order, with explicit title/track/disc/length/artist, and assert the detail
    # endpoint returns the album fields plus a tracklist sorted by (disc, track).
    directory = Path(os.fsdecode(temp_library.directory))

    def _track_item(*, title: str, track: int, disc: int, length: float, artist: str) -> Item:
        item = _make_item(
            directory,
            album="Discovery",
            albumartist="Daft Punk",
            year=2001,
            genre="House",
            title=title,
            track=track,
        )
        item.disc = disc
        item.length = length
        item.artist = artist
        return item

    out_of_order = [
        _track_item(title="Aerodynamic", track=2, disc=1, length=212.5, artist="Daft Punk"),
        _track_item(title="Nightvision", track=1, disc=2, length=104.0, artist="Daft Punk feat. X"),
        _track_item(title="One More Time", track=1, disc=1, length=320.0, artist="Daft Punk"),
        _track_item(title="No Length", track=3, disc=1, length=0.0, artist="Daft Punk"),
    ]
    album = temp_library.add_album(out_of_order)
    album["genre"] = "House"
    album.store()

    resp = client.get(f"/api/albums/{album.id}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["id"] == album.id
    assert body["album_artist"] == "Daft Punk"
    assert body["title"] == "Discovery"
    assert body["year"] == 2001
    assert body["track_count"] == 4
    assert body["genre"] == "House"

    tracks = body["tracks"]
    assert [t["title"] for t in tracks] == [
        "One More Time",
        "Aerodynamic",
        "No Length",
        "Nightvision",
    ]
    assert [(t["disc"], t["track"]) for t in tracks] == [(1, 1), (1, 2), (1, 3), (2, 1)]
    assert tracks[0]["duration_seconds"] == 320.0
    assert tracks[1]["duration_seconds"] == 212.5
    # length 0 maps to None.
    assert tracks[2]["duration_seconds"] is None
    assert tracks[3]["artist"] == "Daft Punk feat. X"


def test_albums_filtered_by_artist(client: TestClient) -> None:
    # ?artist=ABBA returns only ABBA's albums; total reflects the filtered count.
    resp = client.get("/api/albums?artist=ABBA")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["album_artist"] == "ABBA"
    assert body["items"][0]["title"] == "Arrival"


def test_albums_filter_is_exact_match(client: TestClient) -> None:
    # Exact match only: a substring/casefold variant matches nothing.
    assert client.get("/api/albums?artist=abba").json()["total"] == 0
    assert client.get("/api/albums?artist=AB").json()["total"] == 0


def test_albums_filter_paginates_over_filtered_set(
    client: TestClient, temp_library: Library
) -> None:
    # Give ABBA a second album so the filtered set has >1 entry, then paginate it.
    directory = Path(os.fsdecode(temp_library.directory))
    second = temp_library.add_album(
        [
            _make_item(
                directory,
                album="Voulez-Vous",
                albumartist="ABBA",
                year=1979,
                genre="Pop",
                title="Voulez-Vous",
                track=1,
            )
        ]
    )
    second["genre"] = "Pop"
    second.store()

    page1 = client.get("/api/albums?artist=ABBA&limit=1&offset=0").json()
    assert page1["total"] == 2
    assert len(page1["items"]) == 1
    assert page1["items"][0]["title"] == "Arrival"

    page2 = client.get("/api/albums?artist=ABBA&limit=1&offset=1").json()
    assert page2["total"] == 2
    assert len(page2["items"]) == 1
    assert page2["items"][0]["title"] == "Voulez-Vous"


def test_album_detail_missing_album_returns_404(client: TestClient) -> None:
    resp = client.get("/api/albums/999999")
    assert resp.status_code == 404


def test_lifespan_opens_library_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end startup proof: point MUSICDROP_BEETSDIR at a tmp dir holding
    # a hand-written config.yaml + a pre-seeded library.db, then run the
    # lifespan and assert get_library serves the opened handle WITHOUT any
    # dependency override.
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    db_path = tmp_path / "library.db"

    lib = Library(str(db_path), directory=str(music_dir))
    lib.add_album(
        [
            _make_item(
                music_dir,
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

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"directory: {music_dir}\n"
        f"library: {db_path}\n"
        "plugins:\n  - musicbrainz\n"
        "import:\n  autotag: yes\n"
    )

    monkeypatch.setattr(settings, "beets_dir", str(tmp_path))

    app.dependency_overrides.clear()
    with TestClient(app) as client:  # context-manager form runs the lifespan
        assert app.state.beets_library is not None
        resp = client.get("/api/albums")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["album_artist"] == "ABBA"
