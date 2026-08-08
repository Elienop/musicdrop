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
from app.beets.browse import BrowseRow, browse_albums, browse_facets
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
    instrumental_tracks: set[int] | None = None,
    not_instrumental_tracks: set[int] | None = None,
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
        if instrumental_tracks and i in instrumental_tracks:
            it["lyrics_instrumental"] = 1
        if not_instrumental_tracks and i in not_instrumental_tracks:
            # What beets writes on a track it DID find lyrics for; reads back as
            # the string "0", which is truthy in Python.
            it["lyrics_instrumental"] = False
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


def test_invalidate_never_blocks_on_an_in_flight_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I17: invalidate_browse_cache() runs synchronously ON THE EVENT LOOP for
    # every mutation. It must be O(1) even while a threadpool thread is mid-scan
    # — the old single lock made a mutating endpoint freeze the whole asyncio
    # loop for the duration of the rebuild. Orchestrated with events, no sleeps:
    # the build parks inside _build_row until released, and the invalidate runs
    # (in a helper thread so a regression can't hang the suite) while parked.
    import threading

    from app.beets import browse as browse_mod

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    _add(lib, tmp_path, artist="A", album="One", year=2015, genre="Pop", fmt="FLAC")
    browse_mod.invalidate_browse_cache()

    in_build = threading.Event()
    release_build = threading.Event()
    build_calls = {"n": 0}
    real_build = browse_mod._build_row

    def parked_build(album: Any, facts: Any) -> Any:
        build_calls["n"] += 1
        in_build.set()
        assert release_build.wait(timeout=5.0)
        return real_build(album, facts)

    monkeypatch.setattr(browse_mod, "_build_row", parked_build)
    built: list[list[Any]] = []
    builder = threading.Thread(target=lambda: built.append(browse_mod._rows(lib)), daemon=True)
    builder.start()
    assert in_build.wait(timeout=5.0)  # the scan is genuinely in flight

    invalidated = threading.Event()

    def _invalidate_then_signal() -> None:
        browse_mod.invalidate_browse_cache()
        invalidated.set()

    inv = threading.Thread(target=_invalidate_then_signal, daemon=True)
    inv.start()
    # The whole point: invalidation completes WHILE the scan is still parked.
    assert invalidated.wait(timeout=2.0), "invalidate blocked on an in-flight scan"

    release_build.set()
    builder.join(timeout=5.0)
    assert not builder.is_alive()
    assert len(built[0]) == 1  # the requester still gets the rows it built

    # THE DISCARD SEMANTIC: the scan snapshotted the generation BEFORE the
    # mid-flight invalidate bumped it, so its rows must NOT have been stored —
    # they describe a library state that no longer exists. Asserting the cache is
    # EMPTY here is what makes this test able to fail: a store-unconditionally
    # regression leaves the stale rows sitting in _ROWS and is caught right here.
    assert not browse_mod._ROWS, "an invalidated scan's rows were cached anyway"

    # And the next call genuinely re-scans (rather than serving anything stale).
    monkeypatch.setattr(browse_mod, "_build_row", real_build)
    rebuilt = browse_mod._rows(lib)
    assert rebuilt is not built[0]  # a fresh list object, not the discarded one
    assert browse_mod._ROWS  # this build WAS cached (no invalidate raced it)


def test_concurrent_cold_cache_calls_scan_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The build lock's job: two browse calls racing a cold cache must not run
    # duplicate whole-library scans.
    import threading

    from app.beets import browse as browse_mod

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    _add(lib, tmp_path, artist="A", album="One", year=2015, genre="Pop", fmt="FLAC")
    _add(lib, tmp_path, artist="B", album="Two", year=2015, genre="Rock", fmt="MP3")
    browse_mod.invalidate_browse_cache()

    calls = {"n": 0}
    real_build = browse_mod._build_row

    def counting_build(album: Any, facts: Any) -> Any:
        calls["n"] += 1
        return real_build(album, facts)

    monkeypatch.setattr(browse_mod, "_build_row", counting_build)
    results: list[list[Any]] = []
    threads = [
        threading.Thread(target=lambda: results.append(browse_mod._rows(lib)), daemon=True)
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert calls["n"] == 2  # one scan of two albums — NOT four (duplicate scans)
    assert len(results) == 2 and results[0] == results[1]


def test_browse_tolerates_a_non_canonical_per_disc_numbering_value(tmp_path: Path) -> None:
    # A per_disc_numbering value beets tolerates but that isn't a canonical bool
    # (e.g. `on`) makes confuse's `.get(bool)` raise ConfigTypeError; the cache
    # build must read the flag by truthiness instead so it never crashes browse.
    from beets import config

    from app.beets import browse as browse_mod

    lib = Library(str(tmp_path / "library.db"), directory=str(tmp_path / "music"))
    _add(
        lib,
        tmp_path,
        artist="A",
        album="Multi",
        year=2015,
        genre="Pop",
        fmt="FLAC",
        tracks=4,
        discs=2,
        tracktotal=2,  # disctotal=2 -> hits the flag branch
    )
    config["per_disc_numbering"] = "on"  # tolerated by beets, rejected by the bool template
    try:
        browse_mod._ROWS.clear()
        facets = browse_facets(lib)  # builds rows -> _album_tracks_bucket -> reads the flag
        albums, total = browse_albums(lib, genres=[], decades=[], formats=[], limit=50, offset=0)
    finally:
        config["per_disc_numbering"] = False  # restore the beets default

    assert total == 1  # computed without raising ConfigTypeError
    assert {f.value for f in facets.tracks}  # the tracks facet was bucketed
    assert albums[0].track_count == 4


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


def _lyrics_counts(lib: Library) -> dict[str, int]:
    return {v.value: v.count for v in browse_facets(lib).lyrics}


def test_lyrics_facet_counts_instrumentals_as_satisfied(tmp_path: Path) -> None:
    """An instrumental has no lyrics BY NATURE, so it must not read as missing —
    otherwise the album sits at Partial forever with nothing left to fetch.

    Goes through ``browse_facets`` (not the bucket helper) on a real library, so
    it also proves the flex attr survives the cache scan's ``album.items()``
    materialization.
    """
    lib = Library(str(tmp_path / "l.db"), directory=str(tmp_path / "m"))
    _add(lib, tmp_path, artist="A", album="AllInstrumental", tracks=2, instrumental_tracks={1, 2})
    _add(
        lib,
        tmp_path,
        artist="B",
        album="HalfAndHalf",
        tracks=2,
        lyrics_on=1,
        instrumental_tracks={2},
    )
    _add(lib, tmp_path, artist="C", album="OneStillMissing", tracks=2, instrumental_tracks={1})
    _add(lib, tmp_path, artist="D", album="NothingYet", tracks=2)

    assert _lyrics_counts(lib) == {"Complete": 2, "Partial": 1, "Missing": 1}


def test_lyrics_facet_ignores_the_false_instrumental_flag(tmp_path: Path) -> None:
    """beets writes ``lyrics_instrumental`` = False on every track it found lyrics
    for; that reads back as the truthy string "0". A bare truth test would count
    those searched-and-empty tracks as instrumental and report Complete."""
    lib = Library(str(tmp_path / "l.db"), directory=str(tmp_path / "m"))
    _add(lib, tmp_path, artist="A", album="Searched", tracks=2, not_instrumental_tracks={1, 2})

    assert _lyrics_counts(lib) == {"Missing": 1}


def test_lyrics_facet_still_partial_when_only_some_tracks_are_answered(tmp_path: Path) -> None:
    """The bucket stays three-valued: instrumental only ADDS to the satisfied set."""
    lib = Library(str(tmp_path / "l.db"), directory=str(tmp_path / "m"))
    _add(lib, tmp_path, artist="A", album="Three", tracks=3, lyrics_on=1, instrumental_tracks={2})

    assert _lyrics_counts(lib) == {"Partial": 1}


def test_backfill_instrumental_write_invalidates_the_browse_cache(tmp_path: Path) -> None:
    """A backfill that resolves a track as INSTRUMENTAL writes only a flex flag —
    no lyrics text — and that must refresh Browse exactly like a text write does.

    Both writes happen inside the same sweep, whose terminal ``on_complete`` is
    what ``app/api/lyrics.py`` binds to ``emit_library_changed``; this drives the
    real sweep to prove the flag-write inherits that invalidation.
    """
    from beets.util.lyrics import Lyrics

    from app.beets.lyrics import _store_instrumental
    from app.lyrics_jobs.registry import LyricsBackfillRegistry
    from app.lyrics_jobs.runner import sweep
    from app.models.lyrics import ItemLyricsOutcome

    lib = Library(str(tmp_path / "sweep.db"), directory=str(tmp_path / "m"))
    _add(lib, tmp_path, artist="A", album="Solo", tracks=1)
    assert _lyrics_counts(lib) == {"Missing": 1}  # warms the cache

    def _resolve_instrumental(_plugin: Any, item: Any, **_kw: Any) -> ItemLyricsOutcome:
        _store_instrumental(item, Lyrics("", "lrclib", "u"))
        return ItemLyricsOutcome(
            item_id=int(item.id), status="instrumental", source=None, written=False
        )

    reg = LyricsBackfillRegistry()
    reg.start(writes_enabled=False)
    sweep(
        reg,
        make_test_handle(lib, tmp_path),
        delay=0.0,
        write=False,
        fetch_one=_resolve_instrumental,
        make_plugin=lambda **_kw: object(),
        on_complete=lambda: emit_library_changed(SimpleNamespace(state=SimpleNamespace())),
    )

    assert reg.state().phase == "done"
    assert _lyrics_counts(lib) == {"Complete": 1}


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


# ----- characterization: the exact BrowseRow values -----
#
# THE REFEREE for the SQL-aggregate rewrite of the cache build. Every other test
# in this file checks one facet in isolation; this one pins all sixteen
# BrowseRow fields of four albums at once, so a rewrite that quietly shifts a
# bucket edge (format tie-break, genre fallback order, the "0"-truthy
# instrumental trap, per-disc expected-track math) fails here rather than
# shipping. It passed against the per-album ``album.items()`` build and must
# keep passing, UNCHANGED, against the aggregate one.


def _char_item(
    directory: Path,
    *,
    artist: str,
    album: str,
    n: int,
    fmt: str | None = None,
    genre: str | None = None,
    lyrics: str | None = None,
    instrumental: object | None = None,
    tracktotal: int | None = None,
    disc: int | None = None,
) -> Item:
    """One item with per-track control the shared ``_add`` helper doesn't offer."""
    it = Item(album=album, albumartist=artist, artist=artist, title=f"T{n}", track=n)
    it.path = os.fsencode(str(directory / f"{artist} - {album} - {n}.x"))
    if fmt is not None:
        it.format = fmt
    if genre is not None:
        it.genre = genre
    if lyrics is not None:
        it.lyrics = lyrics
    if instrumental is not None:
        it["lyrics_instrumental"] = instrumental
    if tracktotal is not None:
        it.tracktotal = tracktotal
    if disc is not None:
        it.disc = disc
    return it


def _characterization_lib(tmp_path: Path) -> Library:
    """The four-album fixture the characterization test pins. See its docstring."""
    lib = Library(str(tmp_path / "char.db"), directory=str(tmp_path / "music"))

    # A - every field populated, everything Complete.
    a = lib.add_album(
        [
            _char_item(
                tmp_path, artist="Alpha", album="Anthem", n=n, fmt="FLAC", lyrics="la", tracktotal=2
            )
            for n in (1, 2)
        ]
    )
    a["genre"] = "Rock"
    a.year = 1994
    a.original_year = 1989
    a.albumtype = "album"
    a["data_source"] = "MusicBrainz"
    a["media"] = "CD"
    a.country = "SE"
    a.added = 100.0
    a.store()

    # B - format majority vote, genre fallback to the FIRST non-empty item genre,
    # lyrics Partial (one real lyric + one instrumental + one neither).
    b = lib.add_album(
        [
            _char_item(tmp_path, artist="Bravo", album="Beacon", n=1, fmt="MP3", tracktotal=5),
            _char_item(
                tmp_path,
                artist="Bravo",
                album="Beacon",
                n=2,
                fmt="MP3",
                genre="Pop",
                lyrics="la",
                tracktotal=5,
            ),
            _char_item(
                tmp_path,
                artist="Bravo",
                album="Beacon",
                n=3,
                fmt="FLAC",
                genre="Jazz",
                instrumental=1,
                tracktotal=5,
            ),
        ]
    )
    # No album-level genre on purpose: an album flex `genre` propagates DOWN to
    # every item on store(), which would erase the per-item fallback this pins.
    b.added = 200.0
    b.store()

    # C - the "0"-is-truthy trap: beets writes lyrics_instrumental=False on every
    # track it searched and found nothing for, and that reads back as "0".
    c = lib.add_album(
        [_char_item(tmp_path, artist="Charlie", album="Cipher", n=1, instrumental=False)]
    )
    c.year = 2003
    c.added = 300.0
    c.store()

    # D - per_disc_numbering: expected = ONE tracktotal per distinct disc
    # (2 + 3 = 5), not the first item's alone (2). 4 present -> Incomplete.
    d = lib.add_album(
        [
            _char_item(
                tmp_path,
                artist="Delta",
                album="Delta Box",
                n=n,
                fmt="FLAC",
                disc=disc,
                tracktotal=tracktotal,
            )
            for n, disc, tracktotal in ((1, 1, 2), (2, 2, 3), (3, 1, 2), (4, 2, 3))
        ]
    )
    d["genre"] = "Metal"
    d.year = 2011
    d.disctotal = 2
    d.added = 400.0
    d.store()
    return lib


# The expected rows, written from the fixture's INTENT rather than copied off a
# run. Album ids are the insertion order of a fresh temp DB (1..4).
_EXPECTED_ROWS = [
    BrowseRow(
        album_id=1,
        artist_key="alpha",
        album_key="anthem",
        albumartist="Alpha",
        added=100.0,
        genre="Rock",
        decade="1980s",  # original_year 1989 wins over year 1994
        format="FLAC",
        album_type="album",
        source="MusicBrainz",
        media="CD",
        country="SE",
        lyrics="Complete",  # 2 of 2 tracks carry lyrics
        tracks="Complete",  # 2 present vs tracktotal 2
        track_count=2,
        genre_raw="Rock",
    ),
    BrowseRow(
        album_id=2,
        artist_key="bravo",
        album_key="beacon",
        albumartist="Bravo",
        added=200.0,
        genre="Pop",  # first non-empty ITEM genre in (disc, track) order
        decade="Unknown",  # no year at all
        format="MP3",  # 2x MP3 beats 1x FLAC
        album_type="Unknown",
        source="Unknown",
        media="Unknown",
        country="Unknown",
        lyrics="Partial",  # 1 real lyric + 1 instrumental answered, 1 not
        tracks="Incomplete",  # 3 present vs tracktotal 5
        track_count=3,
        genre_raw="Pop",
    ),
    BrowseRow(
        album_id=3,
        artist_key="charlie",
        album_key="cipher",
        albumartist="Charlie",
        added=300.0,
        genre="Unknown",
        decade="2000s",
        format="Unknown",  # no item carries a format
        album_type="Unknown",
        source="Unknown",
        media="Unknown",
        country="Unknown",
        lyrics="Missing",  # the "0" flag must NOT read as instrumental
        tracks="Unknown",  # no tracktotal
        track_count=1,
        genre_raw=None,  # NULL, not "Unknown" - the Album model keeps it nullable
    ),
    BrowseRow(
        album_id=4,
        artist_key="delta",
        album_key="delta box",
        albumartist="Delta",
        added=400.0,
        genre="Metal",
        decade="2010s",
        format="FLAC",
        album_type="Unknown",
        source="Unknown",
        media="Unknown",
        country="Unknown",
        lyrics="Missing",
        tracks="Incomplete",  # per-disc expected 2+3=5 vs 4 present
        track_count=4,
        genre_raw="Metal",
    ),
]


def test_rows_characterization(tmp_path: Path) -> None:
    """Pins _build_row's output field-by-field across the SQL-aggregate rewrite.

    A: 2 FLAC tracks, album genre 'Rock', year 1994 + original_year 1989,
       tracktotal 2, both tracks have lyrics -> everything Complete, decade 1980s.
    B: 3 tracks (2 MP3 + 1 FLAC -> MP3 by majority), NO album genre, item genres
       ['', 'Pop', 'Jazz'] -> fallback 'Pop' (first non-empty in disc/track
       order), no year -> decade Unknown, tracktotal 5 -> Incomplete, one track
       with lyrics + one flagged instrumental + one neither -> lyrics Partial.
    C: 1 track, no format, no tracktotal -> Unknown, no lyrics and
       lyrics_instrumental='0' (the truthy-string trap) -> lyrics Missing.
    D: 4 tracks across 2 discs under per_disc_numbering, per-disc tracktotals
       2 and 3 -> expected 5, present 4 -> Incomplete (using only the FIRST
       item's tracktotal would wrongly read Complete).
    """
    from beets import config as beets_config

    from app.beets import browse as browse_mod

    lib = _characterization_lib(tmp_path)
    beets_config["per_disc_numbering"] = True
    try:
        browse_mod.invalidate_browse_cache()
        rows = sorted(browse_mod._rows(lib), key=lambda r: r.album_id)
    finally:
        beets_config["per_disc_numbering"] = False

    assert rows == _EXPECTED_ROWS


def test_rebuild_issues_no_per_album_items_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold rebuild must not run one ``album.items()`` query per album.

    That N+1 is the whole cost of the cache: ~4.5k queries and 18k beets Item
    materializations on a real library, repaid after EVERY mutation because
    ``emit_library_changed`` drops the cache. The aggregate build reads the item
    facts in a handful of whole-table queries instead, so this spy stays at zero.
    """
    from beets.library import Album as _BeetsAlbum

    from app.beets import browse as browse_mod

    lib = _characterization_lib(tmp_path)
    browse_mod.invalidate_browse_cache()

    calls: list[int] = []
    real_items = _BeetsAlbum.items

    def counting_items(self: _BeetsAlbum, *a: Any, **k: Any) -> Any:
        calls.append(1)
        return real_items(self, *a, **k)

    monkeypatch.setattr(_BeetsAlbum, "items", counting_items)
    facets = browse_facets(lib)  # forces the full rebuild

    assert calls == []  # the aggregate build never calls album.items()
    assert sum(v.count for v in facets.genres) == 4  # and it really built all four rows


def test_item_order_is_play_order_not_the_display_sort(tmp_path: Path) -> None:
    """DELIBERATE CHANGE from the per-album ``album.items()`` build.

    ``album.items()`` returns tracks in beets' ``sort_item`` DISPLAY order
    (``artist+ album+ disc+ track+`` by default), so on a compilation — tracks
    with different artists — the alphabetically-first artist came first, not
    track 1. The order-sensitive facts (genre fallback, a tied format vote,
    which disc's ``tracktotal`` is "first") therefore moved with a user's
    display-sort preference. The aggregate build reads ``(disc, track, id)``
    instead, so a compilation's facets are stable play order.

    Here artist 'Zed' holds track 1 and 'Abe' holds track 2: the old build read
    genre 'Pop' (Abe's, sorted first), this one reads 'Jazz' (track 1's).
    """
    from app.beets import browse as browse_mod

    lib = Library(str(tmp_path / "comp.db"), directory=str(tmp_path / "music"))
    lib.add_album(
        [
            _char_item(tmp_path, artist=artist, album="Comp", n=n, genre=genre)
            for n, artist, genre in ((1, "Zed", "Jazz"), (2, "Abe", "Pop"))
        ]
    ).store()

    browse_mod.invalidate_browse_cache()
    (row,) = browse_mod._rows(lib)
    assert row.genre_raw == "Jazz"  # track 1's genre, NOT the display sort's first


def test_genre_fallback_agrees_across_every_endpoint(tmp_path: Path) -> None:
    """ONE genre answer per album, whichever endpoint asks.

    The Browse cache resolves the item-genre fallback from SQL in play order,
    while ``get_album_detail`` / ``_to_album`` / the duplicates report resolve it
    through ``_album_genre`` over ``list(album.items())`` — which beets returns
    in ``sort_item`` DISPLAY order. On a compilation (tracks with different
    artists) those two orders disagree, so the same album reported one genre on
    the Browse grid and a different one on its own detail page. Pinned here
    because the bug is invisible from inside either endpoint alone.
    """
    from app.beets import browse as browse_mod
    from app.beets.library import _to_album, get_album_detail

    lib = Library(str(tmp_path / "comp.db"), directory=str(tmp_path / "music"))
    # Track 1 is 'Zed'/Jazz, track 2 is 'Abe'/Pop: the display sort puts Abe
    # first, play order puts track 1 first.
    lib.add_album(
        [
            _char_item(tmp_path, artist=artist, album="Comp", n=n, genre=genre)
            for n, artist, genre in ((1, "Zed", "Jazz"), (2, "Abe", "Pop"))
        ]
    ).store()

    browse_mod.invalidate_browse_cache()
    (row,) = browse_mod._rows(lib)
    detail = get_album_detail(lib, row.album_id)
    assert detail is not None
    album = lib.get_album(row.album_id)
    assert album is not None

    assert row.genre_raw == detail.genre == _to_album(album).genre == "Jazz"
