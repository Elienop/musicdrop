import os
from pathlib import Path

from beets.library import Item, Library

from app.beets.playlists import m3u_entries, resolve_tracks


def _lib_with_items(tmp_path: Path) -> tuple[Library, list[int]]:
    music = tmp_path / "music"
    lib = Library(
        str(tmp_path / "library.db"),
        directory=str(music),
        path_formats=[("default", "$albumartist/$album/$track $title")],
    )

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
    tracks = resolve_tracks(lib, [ids[1], ids[0]])  # reversed
    assert [t.title for t in tracks] == ["Beta", "Alpha"]
    assert all(t.available for t in tracks)
    assert tracks[0].duration_seconds == 200.0
    assert tracks[0].artist == "A"
    assert tracks[0].album == "One"


def test_resolve_keeps_duplicates(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    tracks = resolve_tracks(lib, [ids[0], ids[0]])
    assert [t.id for t in tracks] == [ids[0], ids[0]]


def test_missing_id_is_unavailable(tmp_path: Path) -> None:
    lib, ids = _lib_with_items(tmp_path)
    tracks = resolve_tracks(lib, [ids[0], 999_999])
    assert tracks[0].available is True
    assert tracks[1].available is False
    assert tracks[1].id == 999_999
    assert tracks[1].title == ""
    assert tracks[1].duration_seconds is None


def test_empty_ids(tmp_path: Path) -> None:
    lib, _ = _lib_with_items(tmp_path)
    assert resolve_tracks(lib, []) == []


def test_get_item_error_degrades_to_unavailable() -> None:
    class _BoomLib:
        def get_item(self, item_id: int) -> object:
            raise RuntimeError("library is locked")

    tracks = resolve_tracks(_BoomLib(), [7])  # type: ignore[arg-type]  # duck-typed lib
    assert tracks[0].available is False
    assert tracks[0].id == 7


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
