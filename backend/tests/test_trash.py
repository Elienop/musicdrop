"""Tests for the shared reversible-trash primitive + read helpers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from beets.library import Library

from app.beets.library import _abs_path
from app.beets.trash import (
    _album_root,
    _folder_is_shared,
    album_folder,
    album_format_bitrate,
    trash_album,
    trash_album_folder,
)


def test_trash_album_moves_files_and_drops_db(duplicates_lib: Library, tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = int(album.id)

    with duplicates_lib.transaction():
        trash_path = trash_album(duplicates_lib, album, trash_dir=trash)

    # DB row dropped; files relocated under Trash, not destroyed.
    assert duplicates_lib.get_album(album_id) is None
    assert str(trash) in trash_path
    assert os.path.isdir(trash_path)


def test_album_format_bitrate_reads_first_item(duplicates_lib: Library) -> None:
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    fmt, kbps = album_format_bitrate(list(album.items()))
    # The placeholder files carry no real audio header, so format/bitrate are
    # absent — the helper must degrade to (None, None), never raise.
    assert fmt is None or isinstance(fmt, str)
    assert kbps is None or isinstance(kbps, int)


def test_album_folder_is_dirname_of_first_item(duplicates_lib: Library) -> None:
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    folder = album_folder(duplicates_lib, list(album.items()))
    assert folder.endswith("Discovery")


def test_trash_album_folder_takes_whole_folder_incl_sidecars(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = int(album.id)
    src_folder = album_folder(duplicates_lib, list(album.items()))
    # An untracked lyric sidecar next to the tracks — beets has no idea it exists,
    # so the per-item trash would orphan it. The whole-folder move must take it.
    (Path(src_folder) / "01 Track.lrc").write_text("[00:01.00] la", encoding="utf-8")

    with duplicates_lib.transaction():
        dest = trash_album_folder(duplicates_lib, album, trash_dir=trash)

    assert duplicates_lib.get_album(album_id) is None  # dropped from the library
    assert str(trash) in dest and os.path.isdir(dest)
    assert (Path(dest) / "01 Track.lrc").is_file()  # the sidecar came along
    assert not os.path.exists(src_folder)  # no orphaned husk left behind


def test_trash_album_folder_ghost_folder_already_gone_drops_rows(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = int(album.id)
    src_folder = album_folder(duplicates_lib, list(album.items()))
    # The user deleted the album's folder on disk (e.g. over SMB); the DB rows
    # are all that's left. Trashing the "ghost" must drop the rows, not raise
    # FileNotFoundError trying to relocate a folder that no longer exists.
    shutil.rmtree(src_folder)

    with duplicates_lib.transaction():
        dest = trash_album_folder(duplicates_lib, album, trash_dir=trash)

    assert duplicates_lib.get_album(album_id) is None  # ghost rows dropped
    assert str(trash) in dest  # returns the trash dir, nothing actually moved
    assert not trash.exists()  # nothing relocated — there was nothing on disk


def test_folder_shared_guard(duplicates_lib: Library) -> None:
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_root = _album_root(duplicates_lib, list(album.items()))
    music_dir = os.path.normpath(_abs_path(duplicates_lib, duplicates_lib.directory))
    # The album's own folder is exclusively its own -> safe to move wholesale.
    assert _folder_is_shared(duplicates_lib, album, album_root) is False
    # The library root itself must NEVER be wholesale-moved.
    assert _folder_is_shared(duplicates_lib, album, music_dir) is True


# ----- I23: _folder_is_shared must be a scoped query, not a library scan -----


def _album_and_root(lib: Library, artist: str) -> tuple[object, str]:
    album = next(a for a in lib.albums() if a.albumartist == artist)
    return album, _album_root(lib, list(album.items()))


def test_folder_shared_never_materializes_the_item_table(
    duplicates_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The perf property itself: the old code ran lib.items() — constructing a
    # full beets Item for every track in the library, once per trashed album
    # (minutes for delete-artist at 75k tracks, under the swap lock). The check
    # must answer from scoped SQL instead.
    calls = {"n": 0}
    real_items = Library.items

    def counting_items(self: Library, *a: object, **k: object) -> object:
        calls["n"] += 1
        return real_items(self, *a, **k)

    # Resolve album + root BEFORE arming the spy — Album.items() delegates to
    # Library.items internally, and only _folder_is_shared itself is under test.
    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    monkeypatch.setattr(Library, "items", counting_items)
    assert _folder_is_shared(duplicates_lib, album, root) is False
    assert calls["n"] == 0  # no whole-library Item materialization


def test_folder_shared_detects_a_sibling_album_in_the_same_dir(
    duplicates_lib: Library,
) -> None:
    # DATA-LOSS GUARD: two albums with files in ONE dir -> True, else trashing
    # either album's "folder" would take the other's files with it.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = _os.fsencode(_os.path.join(root, "99 Stray.mp3"))
    duplicates_lib.add_album([intruder])
    assert _folder_is_shared(duplicates_lib, album, root) is True


def test_folder_shared_detects_a_sibling_in_a_subfolder(duplicates_lib: Library) -> None:
    # A sibling parked in a SUBFOLDER of the root (e.g. a stray "Disc 2") is
    # collateral all the same.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = _os.fsencode(_os.path.join(root, "Disc 9", "01 Stray.mp3"))
    duplicates_lib.add_album([intruder])
    assert _folder_is_shared(duplicates_lib, album, root) is True


def test_folder_shared_ignores_a_string_prefix_neighbour(duplicates_lib: Library) -> None:
    # ".../Discovery" vs ".../Discovery Deluxe": a byte-prefix match without the
    # separator boundary would flag the neighbour as a sharer (false True is the
    # safe direction, but it would wrongly downgrade every whole-folder trash).
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    neighbour = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    neighbour.path = _os.fsencode(root + " Deluxe" + _os.sep + "01.mp3")
    duplicates_lib.add_album([neighbour])
    assert _folder_is_shared(duplicates_lib, album, root) is False


def test_folder_shared_matches_an_absolute_stored_row(duplicates_lib: Library) -> None:
    # beets stores paths RELATIVE to the library dir in the normal case, but
    # legacy rows can be ABSOLUTE. Both stored forms must be matched — missing
    # the absolute arm would return a false "not shared" for such a row.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = _os.fsencode(_os.path.join(root, "98 Legacy.mp3"))
    duplicates_lib.add_album([intruder])
    with duplicates_lib.transaction() as tx:
        tx.mutate(
            "UPDATE items SET path = ? WHERE id = ?",
            (_os.fsencode(_os.path.join(root, "98 Legacy.mp3")), int(intruder.id)),
        )
    assert _folder_is_shared(duplicates_lib, album, root) is True


def test_folder_shared_weird_path_row_falls_back_to_full_normalization(
    duplicates_lib: Library,
) -> None:
    # DATA-LOSS GUARD for the byte-prefix blind spot: a stored path containing a
    # ".." segment normalizes UNDER the root while its bytes never prefix-match
    # it. The old scan caught this via per-item normpath; the scoped query must
    # route such rows through the same normalization rather than trusting bytes.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = _os.fsencode(_os.path.join(root, "97 Weird.mp3"))
    duplicates_lib.add_album([intruder])
    music_dir = _os.fsdecode(duplicates_lib.directory)
    rel_weird = _os.path.join("Elsewhere", "..", _os.path.relpath(root, music_dir), "97 Weird.mp3")
    with duplicates_lib.transaction() as tx:
        tx.mutate(
            "UPDATE items SET path = ? WHERE id = ?",
            (_os.fsencode(rel_weird), int(intruder.id)),
        )
    assert _folder_is_shared(duplicates_lib, album, root) is True


def test_folder_shared_counts_a_singleton_under_the_root(duplicates_lib: Library) -> None:
    # A singleton item (album_id NULL) parked in the folder is collateral too —
    # the old loop treated it as a sharer via `int(item.album_id or 0)`.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    single = Item(album="", albumartist="", artist="Solo", title="loose", track=0)
    single.path = _os.fsencode(_os.path.join(root, "96 Loose.mp3"))
    duplicates_lib.add(single)  # item WITHOUT an album row
    assert _folder_is_shared(duplicates_lib, album, root) is True
