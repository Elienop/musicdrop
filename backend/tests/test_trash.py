"""Tests for the shared reversible-trash primitive + read helpers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

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
