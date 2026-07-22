"""Faceted Browse — adapter (facets + filtered albums) and the two endpoints.

One representative (genre, decade, format) per album, derived from the album +
its items, so facet counts sum to the album total and AND/OR filtering is
unambiguous. Hermetic temp library; no real files, no network.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.beets.browse import browse_albums, browse_facets
from app.events.broker import EventBroker
from app.events.emit import emit_library_changed
from app.main import app
from tests.conftest import make_test_handle


def _add(
    lib: Library,
    directory: Path,
    *,
    artist: str,
    album: str,
    year: int | None = None,
    genre: str | None = None,
    fmt: str | None = None,
    tracks: int = 2,
    albumtype: str | None = None,
    source: str | None = None,
    media: str | None = None,
    country: str | None = None,
    original_year: int | None = None,
    lyrics_on: int = 0,
    added: float | None = None,
    tracktotal: int = 0,
    discs: int = 1,
) -> None:
    items = []
    for i in range(1, tracks + 1):
        it = Item(album=album, albumartist=artist, artist=artist, title=f"T{i}", track=i)
        it.path = os.fsencode(str(directory / f"{artist} - {album} - {i}.x"))
        if genre is not None:
            it.genre = genre
        if fmt is not None:
            it.format = fmt
        if i <= lyrics_on:
            it.lyrics = "la la la"
        if tracktotal:
            it.tracktotal = tracktotal
        if discs > 1:
            # spread items round-robin across discs; each carries the same
            # per-disc tracktotal
            it.disc = ((i - 1) % discs) + 1
        items.append(it)
    al = lib.add_album(items)
    if genre is not None:
        al["genre"] = genre
    al.year = year or 0
    if albumtype is not None:
        al.albumtype = albumtype
    if source is not None:
        al["data_source"] = source  # flex attr
    if media is not None:
        al["media"] = media  # flex attr
    if country is not None:
        al.country = country
    if original_year is not None:
        al.original_year = original_year
    if added is not None:
        al.added = added
    if discs > 1:
        al.disctotal = discs
    al.store()


@pytest.fixture
def browse_lib(tmp_path: Path) -> Library:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    _add(
        lib,
        tmp_path,
        artist="ABBA",
        album="Arrival",
        year=1976,
        genre="Pop",
        fmt="FLAC",
        albumtype="album",
        source="MusicBrainz",
        media="CD",
        country="SE",
        lyrics_on=2,
    )
    _add(
        lib,
        tmp_path,
        artist="AC/DC",
        album="Back in Black",
        year=1980,
        genre="Rock",
        fmt="MP3",
        albumtype="album",
        source="MusicBrainz",
        media='12" Vinyl',
        country="AU",
        lyrics_on=1,
    )
    _add(
        lib,
        tmp_path,
        artist="Adele",
        album="25",
        year=2015,
        genre="Pop",
        fmt="FLAC",
        albumtype="album",
        source="Deezer",
        media="Digital Media",
        country="GB",
    )
    _add(
        lib,
        tmp_path,
        artist="Metallica",
        album="Ride the Lightning",
        year=2015,
        genre="Metal",
        fmt="FLAC",
        albumtype="album",
        media="CD",
        country="US",
    )
    _add(lib, tmp_path, artist="Mystery", album="Untitled", year=None, genre="Rock", fmt="MP3")
    _add(
        lib,
        tmp_path,
        artist="Quiet",
        album="No Format",
        year=2015,
        genre="Jazz",
        fmt=None,
        albumtype="single",
        country="US",
    )
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


def test_browse_albums_maps_page_without_a_per_album_items_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M22 (N+1): once the BrowseRow cache is warm, mapping a page must NOT run a
    # per-row album.items() query — track_count + genre come from the same scan
    # that built the cache. Before the fix, _to_album re-fetched items per row.
    from beets.library import Album as _BeetsAlbum

    from app.beets import browse as browse_mod

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    _add(lib, tmp_path, artist="A", album="One", year=2015, genre="Pop", fmt="FLAC", tracks=3)
    _add(lib, tmp_path, artist="B", album="Two", year=2015, genre="Rock", fmt="MP3", tracks=5)
    browse_mod._ROWS.clear()
    browse_mod._rows(lib)  # warm the cache — the one legitimate items() scan

    calls = {"n": 0}
    real_items = _BeetsAlbum.items

    def counting_items(self: _BeetsAlbum, *a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return real_items(self, *a, **k)

    monkeypatch.setattr(_BeetsAlbum, "items", counting_items)
    albums, total = browse_albums(lib, genres=[], decades=[], formats=[], limit=50, offset=0)

    assert calls["n"] == 0  # no per-row items() during page mapping — the N+1 is gone
    assert total == 2
    assert {a.title: a.track_count for a in albums} == {"One": 3, "Two": 5}


def test_browse_albums_preserves_null_genre_while_facet_buckets_unknown(tmp_path: Path) -> None:
    # The cached path must keep the Album model's genre NULLABLE (genre_raw), not
    # substitute the facet's "Unknown" default — while the facet still buckets a
    # genre-less album under "Unknown".
    from app.beets import browse as browse_mod

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    _add(lib, tmp_path, artist="A", album="Genred", year=2015, genre="Pop", fmt="FLAC", tracks=2)
    _add(lib, tmp_path, artist="B", album="NoGenre", year=2015, genre=None, fmt="FLAC", tracks=2)
    browse_mod._ROWS.clear()

    albums, _ = browse_albums(lib, genres=[], decades=[], formats=[], limit=50, offset=0)
    by_title = {a.title: a.genre for a in albums}
    assert by_title["Genred"] == "Pop"
    assert by_title["NoGenre"] is None  # model keeps null, NOT "Unknown"

    genre_vals = {f.value for f in browse_facets(lib).genres}
    assert "Unknown" in genre_vals  # facet still buckets the genre-less album


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


# ----- new facets + sort + cache -----


def test_new_facet_counts(browse_lib: Library) -> None:
    f = browse_facets(browse_lib)
    assert {v.value: v.count for v in f.album_types} == {"album": 4, "single": 1, "Unknown": 1}
    assert {v.value: v.count for v in f.sources} == {"MusicBrainz": 2, "Deezer": 1, "Unknown": 3}
    assert {v.value: v.count for v in f.media} == {
        "CD": 2,
        '12" Vinyl': 1,
        "Digital Media": 1,
        "Unknown": 2,
    }
    assert {v.value: v.count for v in f.countries} == {
        "US": 2,
        "SE": 1,
        "AU": 1,
        "GB": 1,
        "Unknown": 1,
    }
    assert {v.value: v.count for v in f.lyrics} == {"Complete": 1, "Partial": 1, "Missing": 4}
    assert sum(v.count for v in f.album_types) == 6  # one value per album


def test_decade_uses_original_year_with_release_fallback(tmp_path: Path) -> None:
    lib = Library(str(tmp_path / "l.db"), directory=str(tmp_path / "m"))
    _add(
        lib,
        tmp_path,
        artist="A",
        album="Remaster",
        year=2011,
        original_year=1984,
        genre="Rock",
        fmt="FLAC",
    )
    _add(lib, tmp_path, artist="B", album="Plain", year=2011, genre="Rock", fmt="FLAC")
    decades = {v.value: v.count for v in browse_facets(lib).decades}
    assert decades == {"2010s": 1, "1980s": 1}


def test_browse_filters_each_new_facet(browse_lib: Library) -> None:
    def total(**kw: list[str]) -> int:
        # kw only carries facet lists (never `sort`), so the list[str] value type is fine.
        _a, t = browse_albums(
            browse_lib,
            genres=[],
            decades=[],
            formats=[],
            limit=50,
            offset=0,
            **kw,  # type: ignore[arg-type]  # facet-only kwargs, never the Literal sort
        )
        return t

    assert total(album_types=["single"]) == 1
    assert total(sources=["Unknown"]) == 3
    assert total(countries=["US"]) == 2
    assert total(lyrics=["Missing"]) == 4
    assert total(medias=["CD"]) == 2


def test_browse_and_across_new_and_old_facets(browse_lib: Library) -> None:
    albums, total = browse_albums(
        browse_lib, genres=["Pop"], decades=[], formats=[], sources=["Deezer"], limit=50, offset=0
    )
    assert total == 1
    assert albums[0].title == "25"


def test_browse_sort_added_newest_first(tmp_path: Path) -> None:
    lib = Library(str(tmp_path / "l.db"), directory=str(tmp_path / "m"))
    _add(lib, tmp_path, artist="A", album="Old", year=2000, genre="Pop", fmt="FLAC", added=100.0)
    _add(lib, tmp_path, artist="B", album="New", year=2001, genre="Pop", fmt="FLAC", added=300.0)
    _add(lib, tmp_path, artist="C", album="Mid", year=2002, genre="Pop", fmt="FLAC", added=200.0)
    albums, _ = browse_albums(
        lib, genres=[], decades=[], formats=[], sort="added", limit=50, offset=0
    )
    assert [a.title for a in albums] == ["New", "Mid", "Old"]


def test_browse_scans_the_library_once(
    browse_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real = Library.albums

    def counting(self: Library, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Library, "albums", counting)
    for _ in range(3):
        browse_facets(browse_lib)
    for _ in range(3):
        browse_albums(browse_lib, genres=["Pop"], decades=[], formats=[], limit=50, offset=0)
    assert calls["n"] == 1  # ONE scan builds the cache; everything reads it


def test_emit_library_changed_drops_the_browse_cache(browse_lib: Library, tmp_path: Path) -> None:
    browse_facets(browse_lib)  # warm the cache
    _add(browse_lib, tmp_path, artist="Zeal", album="Fresh", year=2020, genre="Disco", fmt="FLAC")
    assert "Disco" not in {v.value for v in browse_facets(browse_lib).genres}  # stale until emit
    emit_library_changed(SimpleNamespace(state=SimpleNamespace()))  # no broker -> still invalidates
    assert "Disco" in {v.value for v in browse_facets(browse_lib).genres}


def test_broker_publish_library_changed_drops_the_browse_cache(
    browse_lib: Library, tmp_path: Path
) -> None:
    """The import registry publishes on the broker DIRECTLY (registry.py
    ``_notify_changed``), never passing through ``emit_library_changed`` — the
    broker itself must invalidate, or imports leave Browse stale."""
    loop = asyncio.new_event_loop()
    try:
        broker = EventBroker(loop)
        browse_facets(browse_lib)  # warm the cache
        _add(
            browse_lib, tmp_path, artist="Yara", album="Direct", year=2021, genre="Ska", fmt="FLAC"
        )
        assert "Ska" not in {v.value for v in browse_facets(browse_lib).genres}
        broker.publish_library_changed()
        assert "Ska" in {v.value for v in browse_facets(browse_lib).genres}
    finally:
        loop.close()


def test_facets_endpoint_includes_new_facets(client: TestClient) -> None:
    body = client.get("/api/browse/facets").json()
    assert {v["value"] for v in body["album_types"]} == {"album", "single", "Unknown"}
    assert {v["value"] for v in body["sources"]} == {"MusicBrainz", "Deezer", "Unknown"}
    assert "media" in body
    assert "countries" in body
    assert "lyrics" in body


def test_browse_albums_endpoint_new_filters_and_sort(client: TestClient) -> None:
    assert client.get("/api/browse/albums", params={"album_type": "single"}).json()["total"] == 1
    assert client.get("/api/browse/albums?source=Unknown&country=US").json()["total"] == 2
    # Single-param assertions pin each wire->kwarg mapping independently: every
    # expected total differs from the unfiltered 6 AND from a crossed pair's 0.
    assert client.get("/api/browse/albums", params={"source": "Deezer"}).json()["total"] == 1
    assert client.get("/api/browse/albums", params={"media": "CD"}).json()["total"] == 2
    assert client.get("/api/browse/albums", params={"lyrics": "Complete"}).json()["total"] == 1
    assert client.get("/api/browse/albums", params={"sort": "added"}).status_code == 200


def test_browse_albums_endpoint_rejects_junk_sort(client: TestClient) -> None:
    assert client.get("/api/browse/albums", params={"sort": "loudness"}).status_code == 422


# ----- tracks completeness facet -----


def test_tracks_facet_buckets(tmp_path: Path) -> None:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    # complete: 3 of 3
    _add(lib, tmp_path, artist="A", album="Full", tracks=3, tracktotal=3)
    # incomplete: 2 of 12
    _add(lib, tmp_path, artist="B", album="Partial", tracks=2, tracktotal=12)
    # unknown: as-is import, no tracktotal
    _add(lib, tmp_path, artist="C", album="AsIs", tracks=2)
    facets = browse_facets(lib)
    assert {(v.value, v.count) for v in facets.tracks} == {
        ("Complete", 1),
        ("Incomplete", 1),
        ("Unknown", 1),
    }


def test_tracks_facet_multi_disc_per_disc_numbering(tmp_path: Path) -> None:
    """With per_disc_numbering, expected = one tracktotal per distinct disc."""
    from beets import config as beets_config

    beets_config["per_disc_numbering"] = True
    try:
        lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
        # 2 discs x 2 expected each = 4 expected, 4 present -> Complete
        _add(lib, tmp_path, artist="D", album="Box", tracks=4, tracktotal=2, discs=2)
        # 2 discs x 3 expected each = 6 expected, 4 present -> Incomplete
        _add(lib, tmp_path, artist="E", album="HalfBox", tracks=4, tracktotal=3, discs=2)
        facets = browse_facets(lib)
        by_value = {v.value: v.count for v in facets.tracks}
        assert by_value.get("Complete") == 1
        assert by_value.get("Incomplete") == 1
    finally:
        beets_config["per_disc_numbering"] = False


def test_tracks_facet_whole_missing_disc_undercounts(tmp_path: Path) -> None:
    """DOCUMENTED LIMITATION (inherited from beets' missing plugin): when an
    ENTIRE disc is absent, no item carries that disc's tracktotal, so the
    expected total undercounts and the album reads Complete."""
    from beets import config as beets_config

    beets_config["per_disc_numbering"] = True
    try:
        lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
        # A 2-disc release where all of disc 2 is missing: only disc-1 items
        # exist (2 of 2 for that disc), disctotal says 2 discs — expected
        # collapses to disc 1's total, so the bucket is Complete, not
        # Incomplete. This test pins the behavior so a future fix flips it
        # deliberately.
        _add(lib, tmp_path, artist="F", album="LostDisc", tracks=2, tracktotal=2, discs=1)
        al = lib.albums("album:LostDisc").get()
        assert al is not None
        al.disctotal = 2
        al.store()
        facets = browse_facets(lib)
        by_value = {v.value: v.count for v in facets.tracks}
        assert by_value.get("Complete") == 1
    finally:
        beets_config["per_disc_numbering"] = False


def test_browse_filters_by_tracks_bucket(tmp_path: Path) -> None:
    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    _add(lib, tmp_path, artist="A", album="Full", tracks=3, tracktotal=3)
    _add(lib, tmp_path, artist="B", album="Partial", tracks=2, tracktotal=12)
    albums, total = browse_albums(
        lib,
        genres=[],
        decades=[],
        formats=[],
        tracks=["Incomplete"],
        limit=50,
        offset=0,
    )
    assert total == 1
    assert _names(albums) == {"Partial"}


def test_browse_albums_endpoint_tracks_param(client: TestClient) -> None:
    # browse_lib albums are seeded WITHOUT tracktotal -> every album is Unknown.
    everything = client.get("/api/browse/albums").json()
    unknown = client.get("/api/browse/albums", params={"tracks": ["Unknown"]})
    assert unknown.status_code == 200
    assert unknown.json()["total"] == everything["total"]
    complete = client.get("/api/browse/albums", params={"tracks": ["Complete"]})
    assert complete.json()["total"] == 0
    facets = client.get("/api/browse/facets").json()
    assert "tracks" in facets
