from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.trash import trash_album
from app.beets.trash_manage import (
    empty_all,
    empty_one,
    list_trashed_albums,
    resolve_trash_child,
    restore_album,
)
from tests.conftest import build_library

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
    _tagged_flac_raw(os.fsencode(str(dst)), artist=artist, album=album, title=title, track=track)


def _tagged_flac_raw(dst: bytes, *, artist: str, album: str, title: str, track: int) -> None:
    """Tag a copy of the sample at a RAW path — the name need not be valid UTF-8."""
    if not os.path.exists(dst):
        shutil.copyfile(SAMPLE, os.fsdecode(dst))
    item = Item(album=album, albumartist=artist, artist=artist, title=title, track=track)
    item.path = dst
    item.write()


def _new_library(tmp_path: Path) -> Library:
    return build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))


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
    albums = list_trashed_albums(trash, music_dir=str(tmp_path / "music"))
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
    albums = {a.album: a for a in list_trashed_albums(trash, music_dir=str(tmp_path / "music"))}
    assert set(albums) == {"Amnesiac", "25"}
    # Per-item layout keys on the top dir under trash (the $albumartist dir
    # holding the one album) — still reachable for restore/empty.
    assert albums["Amnesiac"].folder == "Radiohead"
    assert albums["25"].folder == "Adele - 25"  # multi-disc shares one top dir
    assert albums["25"].track_count == 2


def test_trash_album_same_artist_siblings_stay_distinct(tmp_path: Path) -> None:
    # Two DIFFERENT albums by the SAME album-artist, each sent to Trash via the
    # per-item primitive (as every duplicate-resolve does). They must remain TWO
    # reachable listing entries — never collapse under one shared $albumartist top
    # dir, which the whole-folder DELETE would then wipe out wholesale (the sibling
    # the user never saw as its own row = silent data loss).
    lib = _new_library(tmp_path)
    music = tmp_path / "music"
    trash = tmp_path / "trash"

    def add_album(album: str, folder: str, titles: list[str]) -> None:
        items: list[Item] = []
        base = music / folder
        base.mkdir(parents=True, exist_ok=True)
        for i, title in enumerate(titles, start=1):
            dst = base / f"{i:02d} {title}.flac"
            shutil.copyfile(SAMPLE, dst)
            it = Item(
                album=album, albumartist="Portishead", artist="Portishead", title=title, track=i
            )
            it.path = os.fsencode(str(dst))
            it.write()  # persist tags so list_trashed_albums (Item.from_path) reads them
            items.append(it)
        lib.add_album(items).store()

    add_album("Dummy", "Portishead/Dummy", ["Mysterons", "Sour Times"])
    add_album("Third", "Portishead/Third", ["Silence", "Hunter", "Nylon Smile"])
    dummy = next(a for a in lib.albums() if a.album == "Dummy")
    third = next(a for a in lib.albums() if a.album == "Third")

    with lib.transaction():
        trash_album(lib, dummy, trash_dir=trash)
    with lib.transaction():
        trash_album(lib, third, trash_dir=trash)

    albums = list_trashed_albums(trash, music_dir=str(music))
    by_album = {a.album: a for a in albums}
    assert set(by_album) == {"Dummy", "Third"}
    assert len(albums) == 2
    # Each container is exactly one album's worth of audio — no cross-contamination.
    assert by_album["Dummy"].track_count == 2
    assert by_album["Third"].track_count == 3
    # Two distinct top-level entries (distinct DELETE keys), never a shared folder.
    folders = {a.folder for a in albums}
    assert len(folders) == 2


def test_list_keeps_same_tagged_siblings_distinct(tmp_path: Path) -> None:
    # Two trashed copies of the same album (the trash subsystem appends " (n)" on
    # collision) carry identical tags; they must stay TWO reachable rows, never
    # collapse onto folder="." (which the restore/empty guard would 404).
    trash = tmp_path / "trash"
    for sub in ("Dreams", "Dreams (1)"):
        _tagged_flac(
            trash / sub / "01 Dreams.flac",
            artist="2 Brothers",
            album="Dreams",
            title="Dreams",
            track=1,
        )
    albums = list_trashed_albums(trash, music_dir=str(tmp_path / "music"))
    assert {a.folder for a in albums} == {"Dreams", "Dreams (1)"}
    assert all(a.folder != "." for a in albums)


def test_list_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_trashed_albums(tmp_path / "nope", music_dir=str(tmp_path / "music")) == []


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


def test_restore_lands_the_album_when_the_user_config_disables_autotag(tmp_path: Path) -> None:
    # beets swaps the lookup_candidates + user_query stages for import_asis when
    # `import: autotag: no` (session.py run()), and user_query is the ONLY stage
    # that calls choose_match — the sole place an outcome is stashed for the
    # album-id follow-up. Without the worker forcing autotag on, beets imports
    # the album for real (files moved, DB row created) while restore_album sees
    # zero outcomes and reports could_not_restore: a successful restore the UI
    # tells the user failed, with the files already gone from Trash.
    config["import"]["autotag"] = False
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


def test_restore_lands_an_album_whose_folder_name_is_not_valid_utf8(tmp_path: Path) -> None:
    # POSIX names are bytes: b"Old Caf\xe9" is not valid UTF-8, so it reaches the
    # app as a lone surrogate. The whole import path — beets' walk, the task
    # paths, the move — has to stay bytes-exact, or an album is strandable in
    # Trash with no way to get it back. Asserts the FILES landed, not just the
    # status field.
    lib = _new_library(tmp_path)
    trash = tmp_path / "trash"
    trash.mkdir(parents=True)
    raw_folder = os.path.join(os.fsencode(str(trash)), b"Old Caf\xe9")
    os.makedirs(raw_folder)
    _tagged_flac_raw(
        os.path.join(raw_folder, b"01 Dreams.flac"),
        artist="2 Brothers",
        album="Dreams",
        title="Dreams",
        track=1,
    )

    result = restore_album(lib, os.fsdecode(raw_folder), trash_dir=trash)

    assert result.restored is True
    assert result.reason == "restored"
    assert result.album_id is not None
    landed = list((tmp_path / "music" / "2 Brothers" / "Dreams").glob("*.flac"))
    assert len(landed) == 1
    assert not os.listdir(raw_folder)  # moved out of Trash, by the real bytes name


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


def test_resolve_trash_child_refuses_an_overlong_name(tmp_path: Path) -> None:
    # A >255-byte name component: Path.exists() RAISES OSError(ENAMETOOLONG) —
    # it only swallows ENOENT/ENOTDIR/EBADF/ELOOP. The resolver's contract is
    # "unknown child -> ValueError" (the endpoint maps that to its 404), and a
    # name the kernel cannot even stat cannot be a trashed album — so it must
    # take the same refusal path instead of surfacing as a 500.
    trash = tmp_path / "trash"
    trash.mkdir()
    with pytest.raises(ValueError):
        resolve_trash_child(trash, "x" * 300)


def test_empty_one_and_all(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    (trash / "A").mkdir(parents=True)
    (trash / "B").mkdir(parents=True)
    assert empty_one(str(trash / "A")).removed == 1
    assert not (trash / "A").exists()
    assert empty_all(trash).removed == 1  # B remains
    assert list(trash.iterdir()) == []


def test_empty_one_removes_a_loose_file(tmp_path: Path) -> None:
    # A loose audio file directly under trash_dir lists with folder=<filename>;
    # emptying it must unlink the file, not 500 on rmtree (NotADirectoryError).
    trash = tmp_path / "trash"
    trash.mkdir()
    (trash / "loose.flac").write_bytes(b"x")
    assert empty_one(str(trash / "loose.flac")).removed == 1
    assert not (trash / "loose.flac").exists()
