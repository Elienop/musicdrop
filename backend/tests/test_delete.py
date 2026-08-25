"""Tests for the reversible delete (move-to-Trash) of albums and artists."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from beets.library import Library
from fastapi import HTTPException

from app.beets.delete import (
    AlbumNotFoundError,
    delete_album,
    delete_album_op,
    delete_artist,
)
from app.beets.library import _require_id
from app.beets.trash import album_folder


def test_delete_album_trashes_whole_folder_and_drops(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = album_folder(duplicates_lib, list(album.items()))
    (Path(folder) / "cover-extra.lrc").write_text("[00:01.00] x", encoding="utf-8")

    result = delete_album(duplicates_lib, album_id, trash_dir=trash)

    assert result.trashed_albums == 1
    assert str(trash) in result.trash_path
    assert duplicates_lib.get_album(album_id) is None  # dropped from the library
    assert (Path(result.trash_path) / "cover-extra.lrc").is_file()  # sidecar came along
    assert not os.path.exists(folder)  # no orphaned husk


def test_delete_album_ghost_folder_already_gone(duplicates_lib: Library, tmp_path: Path) -> None:
    """Folder deleted on disk outside MusicDrop -> drop the ghost rows, don't 500.

    Reproduces the live user case: an artist folder removed over SMB leaves the
    beets DB rows behind. The front-door delete must clean them, not fail on the
    missing source folder.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = album_folder(duplicates_lib, list(album.items()))
    shutil.rmtree(folder)  # ghost: DB rows remain, the files are gone

    result = delete_album(duplicates_lib, album_id, trash_dir=trash)

    assert result.trashed_albums == 1
    assert duplicates_lib.get_album(album_id) is None  # ghost rows dropped
    assert not trash.exists()  # nothing relocated — there was nothing to move


def test_delete_album_unknown_id_raises(duplicates_lib: Library, tmp_path: Path) -> None:
    with pytest.raises(AlbumNotFoundError):
        delete_album(duplicates_lib, 999_999, trash_dir=tmp_path / "trash")


def test_delete_artist_trashes_all_their_albums(duplicates_lib: Library, tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    before = [a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk"]
    assert before, "fixture should have at least one Daft Punk album"

    result = delete_artist(duplicates_lib, "Daft Punk", trash_dir=trash)

    assert result.trashed_albums == len(before)
    assert not [a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk"]


def test_delete_artist_unknown_is_noop(duplicates_lib: Library, tmp_path: Path) -> None:
    result = delete_artist(duplicates_lib, "Nobody At All", trash_dir=tmp_path / "trash")
    assert result.trashed_albums == 0


def test_delete_album_op_409_during_backfill() -> None:
    """A running library job blocks delete with 409 — before touching the lib."""
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill().start(writes_enabled=True)

    class _App:
        class state:
            beets_library = None

    class _Req:
        app = _App()

    try:
        req = _Req()
        # Calling the async op only CREATES the coroutine — nothing runs (and
        # nothing can raise) until asyncio.run drives it inside the block.
        op = delete_album_op(req, 1)  # type: ignore[arg-type]
        with pytest.raises(HTTPException) as ei:
            asyncio.run(op)
        assert ei.value.status_code == 409
    finally:
        reset_lyrics_backfill()
