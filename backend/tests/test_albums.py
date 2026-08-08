import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from beets.library import Item, Library
from fastapi.concurrency import run_in_threadpool as _real_run_in_threadpool
from fastapi.testclient import TestClient

import app.api.albums as albums_mod
from app.api.albums import get_library
from app.beets.library import (
    _require_id,
    close_library,
    get_album_cover,
    get_album_detail,
    list_albums,
)
from app.config import settings
from app.main import app
from tests.conftest import make_test_handle


def _threadpool_spy(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Record every callable offloaded via the router's run_in_threadpool while
    still executing it (real passthrough). Behaviour is preserved; the spy only
    proves the blocking adapter call left the event loop."""
    spy = Mock(side_effect=lambda fn, *a, **k: _real_run_in_threadpool(fn, *a, **k))
    monkeypatch.setattr(albums_mod, "run_in_threadpool", spy, raising=False)
    return spy


def _offloaded_callables(spy: Mock) -> list[object]:
    return [call.args[0] for call in spy.call_args_list]


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

    albums = sorted(temp_library.albums(), key=lambda a: _require_id(a.id))
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
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["etag"]
    assert resp.content == _TINY_PNG


def test_album_cover_matching_if_none_match_returns_304(
    temp_library: Library, tmp_path: Path
) -> None:
    # The cover must revalidate so a freshly-installed image shows up without a
    # hard refresh; the content-derived ETag keeps that revalidation cheap — an
    # unchanged cover returns a bodiless 304.
    art_file = tmp_path / "cover.png"
    art_file.write_bytes(_TINY_PNG)

    albums = sorted(temp_library.albums(), key=lambda a: _require_id(a.id))
    album = albums[0]
    album["artpath"] = os.fsencode(str(art_file))
    album.store()

    handle = make_test_handle(temp_library, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    try:
        client = TestClient(app)
        first = client.get(f"/api/albums/{album.id}/cover")
        etag = first.headers["etag"]
        second = client.get(
            f"/api/albums/{album.id}/cover",
            headers={"If-None-Match": etag},
        )
    finally:
        app.dependency_overrides.clear()

    assert second.status_code == 304
    assert second.content == b""
    # The 304 re-asserts the validator + cache policy so the cache entry refreshes.
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"] == "no-cache"


def test_album_cover_stale_if_none_match_returns_fresh_bytes(
    temp_library: Library, tmp_path: Path
) -> None:
    # A non-matching validator (e.g. a recycled URL pointing at new bytes) must
    # return the current image, not a 304 — this is the hard-refresh bug.
    art_file = tmp_path / "cover.png"
    art_file.write_bytes(_TINY_PNG)

    albums = sorted(temp_library.albums(), key=lambda a: _require_id(a.id))
    album = albums[0]
    album["artpath"] = os.fsencode(str(art_file))
    album.store()

    handle = make_test_handle(temp_library, tmp_path)
    app.dependency_overrides[get_library] = lambda: handle
    try:
        resp = TestClient(app).get(
            f"/api/albums/{album.id}/cover",
            headers={"If-None-Match": '"stale"'},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
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

    def _track_item(
        *, title: str, track: int, disc: int, length: float, artist: str, fmt: str = ""
    ) -> Item:
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
        item.format = fmt
        return item

    out_of_order = [
        _track_item(title="Aerodynamic", track=2, disc=1, length=212.5, artist="Daft Punk"),
        _track_item(title="Nightvision", track=1, disc=2, length=104.0, artist="Daft Punk feat. X"),
        _track_item(
            title="One More Time", track=1, disc=1, length=320.0, artist="Daft Punk", fmt="FLAC"
        ),
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
    # format comes from the item; empty string maps to None.
    assert tracks[0]["format"] == "FLAC"
    assert tracks[1]["format"] is None


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


def test_list_albums_pages_from_the_browse_cache(
    temp_library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole-library scan/sort happens ONCE, building the shared BrowseRow
    # cache; later pages read that cache and load only the page's albums, never
    # re-scanning lib.albums(). Order + exact artist filter must be preserved.
    calls = {"n": 0}
    real = Library.albums

    def counting(self: Library, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Library, "albums", counting)

    page, total = list_albums(temp_library, limit=50, offset=0)
    list_albums(temp_library, limit=1, offset=1)
    filtered, filtered_total = list_albums(temp_library, limit=50, offset=0, artist="ABBA")

    assert calls["n"] == 1  # ONE scan builds the cache; every page reads it
    # Stable sort preserved: "a-ha" precedes "ABBA" under codepoint ordering.
    assert [a.album_artist for a in page] == ["a-ha", "ABBA"]
    assert total == 2
    # Exact, case-sensitive artist filter, applied over the cached rows.
    assert filtered_total == 1
    assert [a.album_artist for a in filtered] == ["ABBA"]
    assert filtered[0].title == "Arrival"


def test_album_detail_missing_album_returns_404(client: TestClient) -> None:
    resp = client.get("/api/albums/999999")
    assert resp.status_code == 404


def test_lifespan_opens_library_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end startup proof: point settings.beets_dir at a tmp dir holding
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
    close_library(lib)

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


def test_album_detail_exposes_musicbrainz_ids(edit_lib: "Library") -> None:
    from app.beets.library import get_album_detail

    aid = _require_id(next(iter(edit_lib.albums())).id)
    detail = get_album_detail(edit_lib, aid)
    assert detail is not None
    assert detail.mb_albumid == "mb-edit"
    # edit_lib items carry no mb_trackid -> coerced to None, not "".
    assert all(t.mb_trackid is None for t in detail.tracks)


def test_album_detail_exposes_release_identity(temp_library: "Library") -> None:
    # Real beets album: data_source is a flex attr, the rest are fixed fields.
    from app.beets.library import get_album_detail

    album = next(iter(temp_library.albums()))
    album["data_source"] = "MusicBrainz"
    album.label = "Warner Bros. Records"
    album.country = "US"
    album.media = '12" Vinyl'
    album.albumdisambig = "1973 reissue"
    album.mb_albumid = "rel-xyz"
    album.store()

    detail = get_album_detail(temp_library, _require_id(album.id))
    assert detail is not None and detail.release is not None
    r = detail.release
    assert r.data_source == "MusicBrainz"
    assert (r.label, r.country, r.media, r.disambiguation) == (
        "Warner Bros. Records",
        "US",
        '12" Vinyl',
        "1973 reissue",
    )
    assert r.release_url == "https://musicbrainz.org/release/rel-xyz"


def test_list_albums_offloads_scan_to_threadpool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The async endpoint must not run the full sorted(lib.albums()) scan on the
    # event loop — it offloads to the threadpool like browse.py.
    spy = _threadpool_spy(monkeypatch)
    resp = client.get("/api/albums")
    assert resp.status_code == 200
    assert list_albums in _offloaded_callables(spy)


def test_album_detail_offloads_load_to_threadpool(
    client: TestClient, temp_library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    album_id = _require_id(next(iter(temp_library.albums())).id)
    spy = _threadpool_spy(monkeypatch)
    resp = client.get(f"/api/albums/{album_id}")
    assert resp.status_code == 200
    assert get_album_detail in _offloaded_callables(spy)


def test_album_cover_offloads_read_to_threadpool(
    client: TestClient, temp_library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cover read (blocking file open().read() up to 10 MB / MediaFile parse)
    # is offloaded even on the 404 path — the fixture albums carry no artpath.
    album_id = _require_id(next(iter(temp_library.albums())).id)
    spy = _threadpool_spy(monkeypatch)
    resp = client.get(f"/api/albums/{album_id}/cover")
    assert resp.status_code == 404
    assert get_album_cover in _offloaded_callables(spy)


def test_album_detail_track_has_lyrics_flag(temp_library: "Library") -> None:
    """A track with stored lyrics reports has_lyrics=True; one without reports False."""
    from app.beets.library import get_album_detail

    # Pick the multi-track album (ABBA / Arrival) so both the True and False cases
    # are exercised; iteration order otherwise yields the single-track album.
    album = next(a for a in temp_library.albums() if len(a.items()) >= 2)
    album_id = _require_id(album.id)
    items = sorted(album.items(), key=lambda it: it.track)
    items[0].lyrics = "Hello, it's me"
    items[0].store()

    detail = get_album_detail(temp_library, album_id)
    assert detail is not None
    by_track = {t.track: t.has_lyrics for t in detail.tracks}
    assert by_track[1] is True
    assert by_track[2] is False


def _instrumental_by_track(lib: "Library", album_id: int) -> dict[int, bool]:
    detail = get_album_detail(lib, album_id)
    assert detail is not None
    return {t.track: t.instrumental for t in detail.tracks}


def test_album_detail_track_instrumental_flag(temp_library: "Library") -> None:
    """A flagged track reports instrumental=True; the "0" flag beets writes on
    every FOUND track must NOT (it is a truthy Python string)."""
    album = next(a for a in temp_library.albums() if len(a.items()) >= 2)
    album_id = _require_id(album.id)
    items = sorted(album.items(), key=lambda it: it.track)
    items[0]["lyrics_instrumental"] = 1
    items[0].store()
    items[1]["lyrics_instrumental"] = False  # beets' "searched, found real lyrics" value
    items[1].store()

    by_track = _instrumental_by_track(temp_library, album_id)
    assert by_track[1] is True
    assert by_track[2] is False


def test_album_detail_track_instrumental_defaults_false_without_the_flag(
    temp_library: "Library",
) -> None:
    """A track beets never searched carries no flex row at all -> not instrumental."""
    album = next(a for a in temp_library.albums() if len(a.items()) >= 2)
    by_track = _instrumental_by_track(temp_library, _require_id(album.id))
    assert by_track == {1: False, 2: False}


def test_album_detail_real_lyrics_beat_a_stale_instrumental_flag(temp_library: "Library") -> None:
    """Non-empty lyrics win over the flag — the same precedence the coverage SQL's
    mutually-exclusive buckets use. The two fields are never both true."""
    album = next(a for a in temp_library.albums() if len(a.items()) >= 2)
    album_id = _require_id(album.id)
    item = sorted(album.items(), key=lambda it: it.track)[0]
    item.lyrics = "Hello, it's me"
    item["lyrics_instrumental"] = 1  # stale verdict left behind by an earlier sweep
    item.store()

    detail = get_album_detail(temp_library, album_id)
    assert detail is not None
    track = next(t for t in detail.tracks if t.track == 1)
    assert track.has_lyrics is True
    assert track.instrumental is False


def test_instrumental_predicate_has_exactly_one_implementation() -> None:
    """The lyrics adapter, the album mapper and the browse facet MUST test the flag
    the same way. A second copy is how the "0"-is-truthy bug comes back on one path
    only, so pin that every consumer names the SAME function object (``vars`` rather
    than attribute access: the name is private, so mypy forbids reading it off a
    module that only re-exports it).

    ``_instrumental_value`` is the one implementation of the flag's truth.
    ``_is_instrumental`` is the item-shaped wrapper the beets-object paths use;
    ``browse`` reads the flag straight out of ``item_attributes`` in its aggregate
    cache build, so it consumes the value predicate directly."""
    from app.beets import browse as browse_mod
    from app.beets import library as library_mod
    from app.beets import lyrics as lyrics_mod

    predicate = vars(library_mod)["_instrumental_value"]
    assert vars(browse_mod)["_instrumental_value"] is predicate

    on_item = vars(library_mod)["_is_instrumental"]
    assert vars(lyrics_mod)["_is_instrumental"] is on_item
    # The item wrapper must not re-derive the truth: the "0"-is-truthy trap and
    # its opposite have to read identically through both entry points.
    for raw, expected in (("0", False), ("", False), ("false", False), ("1", True)):
        assert predicate(raw) is expected
        assert on_item(SimpleNamespace(get=lambda _key, v=raw: v)) is expected
