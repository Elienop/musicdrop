from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.trash_manage import (
    empty_all,
    empty_one,
    list_trashed_albums,
    resolve_trash_child,
    restore_album,
)

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


@pytest.fixture(autouse=True)
def _serial() -> Iterator[None]:
    # Imports run single-threaded; the starter's copy:yes is the manual default
    # (run_import_worker snapshots/restores move/copy around each run).
    config["threaded"] = False
    config["import"]["copy"] = True
    config["import"]["move"] = False
    yield
    config["threaded"] = False


def _tagged_flac(dst: Path, *, artist: str, album: str, title: str, track: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SAMPLE, dst)
    item = Item(album=album, albumartist=artist, artist=artist, title=title, track=track)
    item.path = os.fsencode(str(dst))
    item.write()


def _new_library(tmp_path: Path) -> Library:
    return Library(
        str(tmp_path / "library.db"),
        directory=str(tmp_path / "music"),
        path_formats=[("default", "$albumartist/$album/$track $title")],
    )


def test_list_groups_whole_folder_album(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    _tagged_flac(
        trash / "2 Brothers - Dreams" / "01 Dreams.flac",
        artist="2 Brothers",
        album="Dreams",
        title="Dreams",
        track=1,
    )
    _tagged_flac(
        trash / "2 Brothers - Dreams" / "02 Come.flac",
        artist="2 Brothers",
        album="Dreams",
        title="Come",
        track=2,
    )
    albums = list_trashed_albums(trash)
    assert len(albums) == 1
    assert albums[0].album_artist == "2 Brothers"
    assert albums[0].album == "Dreams"
    assert albums[0].track_count == 2
    assert albums[0].folder == "2 Brothers - Dreams"
    assert albums[0].format == "FLAC"


def test_list_groups_per_item_layout_and_multidisc(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    # per-item fallback layout: trash/$albumartist/$album/...
    _tagged_flac(
        trash / "Radiohead" / "Amnesiac" / "01 A.flac",
        artist="Radiohead",
        album="Amnesiac",
        title="A",
        track=1,
    )
    # multi-disc: two CD folders, one album
    _tagged_flac(
        trash / "Adele - 25" / "CD1" / "01 a.flac", artist="Adele", album="25", title="a", track=1
    )
    _tagged_flac(
        trash / "Adele - 25" / "CD2" / "01 b.flac", artist="Adele", album="25", title="b", track=1
    )
    albums = {a.album: a for a in list_trashed_albums(trash)}
    assert set(albums) == {"Amnesiac", "25"}
    assert albums["Amnesiac"].folder == os.path.join("Radiohead", "Amnesiac")
    assert albums["25"].folder == "Adele - 25"  # commonpath of CD1/CD2
    assert albums["25"].track_count == 2


def test_list_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_trashed_albums(tmp_path / "nope") == []


def test_restore_imports_as_is_and_empties_folder(tmp_path: Path) -> None:
    lib = _new_library(tmp_path)
    trash = tmp_path / "trash"
    folder = trash / "2 Brothers - Dreams"
    _tagged_flac(
        folder / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )

    result = restore_album(lib, str(folder), trash_dir=trash)

    assert result.restored is True
    assert result.reason == "restored"
    assert result.album_id is not None
    landed = list((tmp_path / "music" / "2 Brothers" / "Dreams").glob("*.flac"))
    assert len(landed) == 1
    assert not list(folder.rglob("*.flac"))  # moved out of Trash


def test_restore_duplicate_skips_and_keeps_files(tmp_path: Path) -> None:
    lib = _new_library(tmp_path)
    # An album with the same identity is already in the library.
    existing = Item(
        album="Dreams", albumartist="2 Brothers", artist="2 Brothers", title="Dreams", track=1
    )
    existing.path = os.fsencode(
        str(tmp_path / "music" / "2 Brothers" / "Dreams" / "01 Dreams.flac")
    )
    lib.add_album([existing])

    trash = tmp_path / "trash"
    folder = trash / "2 Brothers - Dreams"
    _tagged_flac(
        folder / "01 Dreams.flac", artist="2 Brothers", album="Dreams", title="Dreams", track=1
    )

    result = restore_album(lib, str(folder), trash_dir=trash)

    assert result.restored is False
    assert result.reason == "already_in_library"
    assert list(folder.rglob("*.flac"))  # still in Trash, untouched


def test_resolve_trash_child_guards_traversal(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    assert resolve_trash_child(trash, "Album") == (trash / "Album").resolve()
    with pytest.raises(ValueError):
        resolve_trash_child(trash, "../escape")
    with pytest.raises(ValueError):
        resolve_trash_child(trash, "missing")
    with pytest.raises(ValueError):
        resolve_trash_child(trash, ".")  # the Trash root itself


def test_empty_one_and_all(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    (trash / "A").mkdir(parents=True)
    (trash / "B").mkdir(parents=True)
    assert empty_one(str(trash / "A")).removed == 1
    assert not (trash / "A").exists()
    assert empty_all(trash).removed == 1  # B remains
    assert list(trash.iterdir()) == []
