import os
from pathlib import Path

from beets.library import Item, Library

from app.beets.playlists import TrackRef, m3u_entries, resolve_entries, track_match_refs
from app.models.playlist import PendingTrack
from app.playlists.store import StoredEntry
from tests.conftest import build_library


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
    return lib, [int(a.id), int(b.id)]


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


def test_get_item_error_degrades_to_unavailable() -> None:
    class _BoomLib:
        def get_item(self, item_id: int) -> object:
            raise RuntimeError("library is locked")

    tracks = resolve_entries(_BoomLib(), [StoredEntry(uid="u", item_id=7)])  # type: ignore[arg-type]  # duck-typed lib
    assert tracks[0].available is False
    assert tracks[0].id == 7
    assert tracks[0].uid == "u"


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


def test_track_match_refs_track_zero_becomes_none(tmp_path: Path) -> None:
    music = tmp_path / "music"
    lib = Library(str(tmp_path / "library.db"), directory=str(music))
    music.mkdir(parents=True, exist_ok=True)
    f = music / "x.flac"
    f.write_bytes(b"\x00")
    it = Item(album="Al", albumartist="Ar", artist="Ar", title="T", track=0, length=10.0)
    it.path = os.fsencode(str(f))
    lib.add(it)
    refs = track_match_refs(lib, [int(it.id)])
    assert refs[0].track is None  # absent/zero track number -> None
