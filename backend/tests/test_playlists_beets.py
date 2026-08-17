import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from beets.dbcore.query import Query
from beets.dbcore.sort import Sort
from beets.library import Item, Library

from app.beets.library import _require_id
from app.beets.playlists import (
    TrackRef,
    cover_album_ids,
    m3u_entries,
    resolve_entries,
    track_match_refs,
)
from app.models.playlist import PendingTrack
from app.playlists.store import StoredEntry
from tests.conftest import build_library, make_test_handle


def _lib_with_items(tmp_path: Path) -> tuple[Library, list[int]]:
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(folder: str, fname: str, **fields: object) -> Item:
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        f = base / fname
        f.write_bytes(b"\x00")
        it = Item(**fields)  # type: ignore[arg-type]  # beets Item kwargs are untyped
        it.path = os.fsencode(str(f))
        return it

    a = add(
        "A/One",
        "01.flac",
        album="One",
        albumartist="A",
        artist="A",
        title="Alpha",
        track=1,
        length=100.0,
    )
    b = add(
        "A/One",
        "02.flac",
        album="One",
        albumartist="A",
        artist="A",
        title="Beta",
        track=2,
        length=200.0,
    )
    lib.add_album([a, b])
    return lib, [_require_id(a.id), _require_id(b.id)]


def _lib_with_albums_and_singleton(tmp_path: Path) -> tuple[object, dict[str, int]]:
    """Two 2-track albums (A, B) plus one album-less singleton, wrapped in a
    ``LibraryHandle`` (what ``cover_album_ids`` takes)."""
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(folder: str, fname: str, **fields: object) -> Item:
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        f = base / fname
        f.write_bytes(b"\x00")
        it = Item(**fields)  # type: ignore[arg-type]  # beets Item kwargs are untyped
        it.path = os.fsencode(str(f))
        return it

    a1 = add("A/One", "01.flac", album="One", albumartist="A", artist="A", title="Alpha", track=1)
    a2 = add("A/One", "02.flac", album="One", albumartist="A", artist="A", title="Beta", track=2)
    alb_a = lib.add_album([a1, a2])
    b1 = add("B/Two", "01.flac", album="Two", albumartist="B", artist="B", title="Gamma", track=1)
    alb_b = lib.add_album([b1])
    # Album-less singleton: added via lib.add (not add_album), so album_id is 0.
    sdir = music / "loose"
    sdir.mkdir(parents=True, exist_ok=True)
    sf = sdir / "z.flac"
    sf.write_bytes(b"\x00")
    si = Item(artist="C", albumartist="C", title="Solo", track=1)
    si.path = os.fsencode(str(sf))
    lib.add(si)
    handle = make_test_handle(lib, tmp_path)
    return handle, {
        "a1": _require_id(a1.id),
        "a2": _require_id(a2.id),
        "b1": _require_id(b1.id),
        "solo": _require_id(si.id),
        "album_a": _require_id(alb_a.id),
        "album_b": _require_id(alb_b.id),
    }


def test_cover_album_ids_distinct_first_appearance_order(tmp_path: Path) -> None:
    handle, ids = _lib_with_albums_and_singleton(tmp_path)
    # a1, a2 share album A; b1 is album B; a1 again is a dup; solo is a singleton;
    # 999999 is unknown. Expect [album A, album B] in first-appearance order.
    result = cover_album_ids(
        handle,  # type: ignore[arg-type]  # duck-typed handle
        [ids["a1"], ids["a2"], ids["b1"], ids["a1"], ids["solo"], 999999],
    )
    assert result == [ids["album_a"], ids["album_b"]]


def test_cover_album_ids_respects_limit(tmp_path: Path) -> None:
    handle, ids = _lib_with_albums_and_singleton(tmp_path)
    result = cover_album_ids(handle, [ids["a1"], ids["b1"]], limit=1)  # type: ignore[arg-type]
    assert result == [ids["album_a"]]


def test_cover_album_ids_bounds_lookups_by_scan_cap() -> None:
    """scan_cap bounds the TOTAL get_item attempts, even when every id is
    unresolvable — otherwise a long all-unknown list does one lookup per id."""

    class _CountingLib:
        def __init__(self) -> None:
            self.calls = 0

        def get_item(self, item_id: int) -> None:
            self.calls += 1
            return None  # every id is unknown

    class _Handle:
        def __init__(self, lib: _CountingLib) -> None:
            self.lib = lib

    lib = _CountingLib()
    handle = _Handle(lib)
    result = cover_album_ids(handle, list(range(100)), scan_cap=10)  # type: ignore[arg-type]
    assert result == []
    assert lib.calls <= 10


def test_cover_album_ids_skips_singleton_and_unknown(tmp_path: Path) -> None:
    handle, ids = _lib_with_albums_and_singleton(tmp_path)
    assert cover_album_ids(handle, [ids["solo"], 999999]) == []  # type: ignore[arg-type]


def test_resolve_preserves_order(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    entries = [StoredEntry(uid="u1", item_id=ids[1]), StoredEntry(uid="u2", item_id=ids[0])]
    tracks = resolve_entries(lib, entries)  # reversed
    assert [t.title for t in tracks] == ["Beta", "Alpha"]
    assert [t.uid for t in tracks] == ["u1", "u2"]
    assert all(t.available for t in tracks)
    assert all(not t.pending for t in tracks)
    assert tracks[0].duration_seconds == 200.0
    assert tracks[0].artist == "A"
    assert tracks[0].album == "One"


def test_resolve_keeps_duplicates(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    entries = [StoredEntry(uid="a", item_id=ids[0]), StoredEntry(uid="b", item_id=ids[0])]
    tracks = resolve_entries(lib, entries)
    assert [t.id for t in tracks] == [ids[0], ids[0]]
    assert [t.uid for t in tracks] == ["a", "b"]  # each occurrence keeps its own uid


def test_missing_id_is_unavailable(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    entries = [StoredEntry(uid="ok", item_id=ids[0]), StoredEntry(uid="gone", item_id=999_999)]
    tracks = resolve_entries(lib, entries)
    assert tracks[0].available is True
    assert tracks[1].available is False
    assert tracks[1].pending is False
    assert tracks[1].id == 999_999
    assert tracks[1].title == ""
    assert tracks[1].duration_seconds is None


def test_empty_entries(tmp_path: Path) -> None:
    lib, _ = _lib_with_items(tmp_path)
    assert resolve_entries(lib, []) == []


def test_query_error_degrades_to_unavailable() -> None:
    class _BoomLib:
        def items(self, query: object) -> object:
            raise RuntimeError("library is locked")

    tracks = resolve_entries(_BoomLib(), [StoredEntry(uid="u", item_id=7)])  # type: ignore[arg-type]  # duck-typed lib
    assert tracks[0].available is False
    assert tracks[0].id == 7
    assert tracks[0].uid == "u"


def test_resolve_entries_uses_one_batched_query_not_per_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perf: resolve_entries must issue ONE batched query for all entries, never a
    lib.get_item per entry (the old N+1 that stalled a few-thousand-entry playlist)."""
    lib, ids = _lib_with_items(tmp_path)
    counts = {"items": 0}
    real_items = lib.items

    def spy_items(
        query: str | Sequence[str] | Query | None = None, sort: Sort | None = None
    ) -> object:
        counts["items"] += 1
        return real_items(query, sort)

    def boom_get_item(item_id: int) -> object:
        raise AssertionError("resolve_entries must not call get_item per entry")

    monkeypatch.setattr(lib, "items", spy_items)
    monkeypatch.setattr(lib, "get_item", boom_get_item)
    entries = [StoredEntry(uid=f"u{i}", item_id=item_id) for i, item_id in enumerate(ids)]
    tracks = resolve_entries(lib, entries)
    assert [t.available for t in tracks] == [True, True]  # both resolved via the batch
    assert counts["items"] == 1  # single query (<500 ids -> one chunk), not per-entry


def test_resolve_entries_interleaves_pending_rows(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    entries = [
        StoredEntry(uid="u1", item_id=ids[0]),
        StoredEntry(uid="u2", pending=PendingTrack(artist="X", title="Lost", source="line")),
        StoredEntry(uid="u3", item_id=999999),  # gone from the library
    ]
    tracks = resolve_entries(lib, entries)
    assert [t.uid for t in tracks] == ["u1", "u2", "u3"]
    assert tracks[0].id == ids[0] and tracks[0].available and not tracks[0].pending
    assert tracks[1].id is None and tracks[1].pending and not tracks[1].available
    assert tracks[1].title == "Lost" and tracks[1].artist == "X"
    # The pending row carries the original source text (for a bare-path m3u
    # entry it's the only "it was this" identity); resolved/unavailable rows
    # have no source.
    assert tracks[1].source == "line"
    assert tracks[0].source is None
    assert tracks[2].source is None
    assert tracks[2].id == 999999 and not tracks[2].available and not tracks[2].pending


def test_m3u_entries_relative_paths(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    export_dir = str(tmp_path / "music" / ".playlists")
    entries = m3u_entries(lib, ids, export_dir)
    assert [e.title for e in entries] == ["Alpha", "Beta"]
    # tracks live at <music>/A/One/0X.flac; export dir is <music>/.playlists
    assert entries[0].path == "../A/One/01.flac"
    assert entries[0].duration_seconds == 100
    assert entries[0].artist == "A"


def test_m3u_entries_skips_unavailable(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    export_dir = str(tmp_path / "music" / ".playlists")
    entries = m3u_entries(lib, [ids[0], 999_999, ids[1]], export_dir)
    # the missing id has no file path, so it is dropped from the .m3u8
    assert [e.title for e in entries] == ["Alpha", "Beta"]


def test_m3u_entries_empty(tmp_path: Path) -> None:
    lib, _ = _lib_with_items(tmp_path)
    assert m3u_entries(lib, [], str(tmp_path / "music" / ".playlists")) == []


def test_track_match_refs_ordered_with_metadata(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    refs = track_match_refs(lib, [ids[1], ids[0]])  # reversed
    # Each ref carries ITS OWN beets id: a Plex miss is reported against a row,
    # so an id paired with the wrong track would point the UI at the wrong one.
    assert [r.item_id for r in refs] == [ids[1], ids[0]]
    assert [r.title for r in refs] == ["Beta", "Alpha"]
    assert [r.track for r in refs] == [2, 1]
    assert refs[0].albumartist == "A"
    assert refs[0].album == "One"
    # absolute, order-preserving paths (what translate_path will consume)
    assert all(isinstance(r, TrackRef) for r in refs)
    assert all(os.path.isabs(r.abs_path) for r in refs)
    assert refs[0].abs_path.endswith("02.flac")
    assert refs[1].abs_path.endswith("01.flac")


def test_track_match_refs_skips_missing(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    refs = track_match_refs(lib, [ids[0], 999_999])
    assert len(refs) == 1
    assert refs[0].title == "Alpha"
    assert refs[0].item_id == ids[0]  # a dropped id must not shift the pairing


def test_track_match_refs_track_zero_becomes_none(tmp_path: Path) -> None:
    music = tmp_path / "music"
    lib = Library(str(tmp_path / "library.db"), directory=str(music))
    music.mkdir(parents=True, exist_ok=True)
    f = music / "x.flac"
    f.write_bytes(b"\x00")
    it = Item(album="Al", albumartist="Ar", artist="Ar", title="T", track=0, length=10.0)
    it.path = os.fsencode(str(f))
    lib.add(it)
    refs = track_match_refs(lib, [_require_id(it.id)])
    assert refs[0].track is None  # absent/zero track number -> None


def test_track_match_refs_carry_length_for_the_plex_duration_guard(tmp_path: Path) -> None:
    # Plex's (album, title) fallback accepts a candidate only when its length
    # agrees, so a ref built without one silently disables that fallback for the
    # track -- and the ref has to carry ITS OWN length, not the neighbour's.
    lib, ids = _lib_with_items(tmp_path)
    refs = track_match_refs(lib, [ids[1], ids[0]])  # reversed
    assert [r.length_seconds for r in refs] == [200.0, 100.0]


def test_track_match_refs_zero_length_becomes_none(tmp_path: Path) -> None:
    # A length beets never read is 0.0 in the DB. The Plex side has to see that
    # as ABSENT: taken literally, a zero-second track agrees with every other
    # zero-second track and the duration guard stops guarding.
    music = tmp_path / "music"
    lib = Library(str(tmp_path / "library.db"), directory=str(music))
    music.mkdir(parents=True, exist_ok=True)
    f = music / "z.flac"
    f.write_bytes(b"\x00")
    it = Item(album="Al", albumartist="Ar", artist="Ar", title="T", track=1, length=0.0)
    it.path = os.fsencode(str(f))
    lib.add(it)
    refs = track_match_refs(lib, [_require_id(it.id)])
    assert refs[0].length_seconds is None
