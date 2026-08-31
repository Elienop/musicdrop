"""The origin record a trashed folder carries, and the move-back restore on it.

Covers the invariant the feature exists for: a folder MusicDrop moves to Trash
must carry enough to be put back exactly where it came from — and recording that
must never be able to make a delete fail.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets import config
from beets.library import Album, Item, Library

from app.beets.import_session import ImportBridge, WebImportSession, run_import_worker
from app.beets.library import LibraryRootUnavailableError, _require_id
from app.beets.trash import trash_album, trash_album_folder, trash_folder
from app.beets.trash_manage import (
    TrashRestoreIncompleteError,
    _return_to_trash,
    list_trashed_albums,
    restore_album,
)
from app.beets.trash_record import (
    RECORD_NAME,
    TrashOrigin,
    move_back_target,
    read_trash_origin,
    write_trash_origin,
)
from app.models.bank import BankApplyDirective
from tests.conftest import build_library

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


@pytest.fixture(autouse=True)
def _serial() -> Iterator[None]:
    # Same posture as test_trash_manage: single-threaded, and the starter's
    # copy:yes as the manual default (run_import_worker snapshots/restores it).
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


def _seeded_library(tmp_path: Path, *, folder: str) -> Library:
    """A library with the album under test at ``music/<folder>``, plus a bystander.

    ``folder`` is deliberately a parameter: the whole point of the origin record
    is an album whose real folder is NOT what the path template would produce.

    The bystander album is not decoration. Trashing the album under test empties
    the music root when it is the only one, and an empty root is exactly what a
    dropped share looks like — ``require_library_root`` refuses it, so a
    one-album library would make every restore here fail for a reason no real
    library has. It is also the album ``require_library_present`` samples to
    prove the share is really there.
    """
    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, at: str, titles: list[str]) -> None:
        items: list[Item] = []
        for i, title in enumerate(titles, start=1):
            dst = music / at / f"{i:02d} {title}.flac"
            _tagged_flac(dst, artist=artist, album=album, title=title, track=i)
            item = Item(album=album, albumartist=artist, artist=artist, title=title, track=i)
            item.path = os.fsencode(str(dst))
            items.append(item)
        lib.add_album(items).store()

    add(artist="Portishead", album="Dummy", at=folder, titles=["Mysterons", "Sour Times"])
    add(artist="Radiohead", album="Amnesiac", at="Radiohead/Amnesiac", titles=["Packt"])
    return lib


def _dummy(lib: Library) -> Album:
    """The album under test, never the bystander."""
    return next(a for a in lib.albums() if a.album == "Dummy")


def _record(entry: Path) -> TrashOrigin:
    record = read_trash_origin(entry)
    assert record is not None
    return record


# ----- the record is written by every mover that relocates something -----


def test_trash_album_folder_records_the_folder_it_came_from(tmp_path: Path) -> None:
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    source = str(tmp_path / "music" / "Portishead" / "Dummy")
    album = _dummy(lib)

    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))

    record = _record(dest)
    assert record.origin == source
    assert record.moved == "folder"  # a whole directory moved -> a move-back is exact


def test_trash_folder_records_the_husk_origin(tmp_path: Path) -> None:
    # The husk case is the one with NO other exit from Trash: an audio-free
    # art/booklet folder cannot be imported, so before the record its only
    # remaining option was permanent deletion.
    husk = tmp_path / "music" / "Portishead" / "Dummy"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")

    dest = trash_folder(husk, trash_dir=tmp_path / "trash")

    record = _record(dest)
    assert record.origin == str(husk)
    assert record.moved == "folder"


def test_trash_album_records_the_source_folder_as_items(tmp_path: Path) -> None:
    # The per-item mover takes tracked FILES out of a folder that may hold other
    # music, so the origin is recorded for display but a move-back is not on
    # offer — see trash_record.MovedShape.
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    source = str(tmp_path / "music" / "Portishead" / "Dummy")
    album = _dummy(lib)

    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash")

    container = next(p for p in (tmp_path / "trash").iterdir() if p.is_dir())
    record = _record(container)
    assert record.origin == source
    assert record.moved == "items"
    assert move_back_target(record, music_dir=str(tmp_path / "music")) is None


# ----- recording must never be able to fail a delete -----


def test_write_trash_origin_swallows_a_failing_write(tmp_path: Path) -> None:
    # The binding secondary invariant: this is a NEW write on the delete path,
    # so its failure must be no worse than today (folder in Trash, no record).
    write_trash_origin(tmp_path / "does-not-exist", origin="/music/A/B", moved="folder")
    assert not (tmp_path / "does-not-exist").exists()


def test_a_husk_still_reaches_trash_when_the_record_cannot_be_written(tmp_path: Path) -> None:
    # A real failure, no monkeypatching: a DIRECTORY already sitting at the
    # sidecar's name travels with the move, so write_text hits IsADirectoryError
    # on the destination. The delete must still complete.
    husk = tmp_path / "music" / "Old Name"
    (husk / RECORD_NAME).mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")

    dest = trash_folder(husk, trash_dir=tmp_path / "trash")

    assert dest.is_dir()
    assert (dest / "cover.jpg").is_file()
    assert not husk.exists()
    assert read_trash_origin(dest) is None  # no record, but the delete happened


def test_an_album_still_reaches_trash_when_the_record_cannot_be_written(tmp_path: Path) -> None:
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    source = tmp_path / "music" / "Portishead" / "Dummy"
    (source / RECORD_NAME).mkdir()
    album = _dummy(lib)
    album_id = _require_id(album.id)

    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))

    assert lib.get_album(album_id) is None  # the delete completed
    assert len(list(dest.glob("*.flac"))) == 2
    assert read_trash_origin(dest) is None


# ----- reading a record is reading untrusted input -----


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json at all", id="unparseable"),
        pytest.param(json.dumps([1, 2, 3]), id="not-an-object"),
        pytest.param(
            json.dumps({"schema": 2, "origin": "/music/A", "moved": "folder"}), id="future-schema"
        ),
        pytest.param(
            json.dumps({"schema": 1, "origin": "music/A", "moved": "folder"}), id="relative-origin"
        ),
        pytest.param(
            json.dumps({"schema": 1, "origin": "/music/A", "moved": "sideways"}), id="unknown-shape"
        ),
        pytest.param(json.dumps({"schema": 1, "moved": "folder"}), id="no-origin"),
    ],
)
def test_read_trash_origin_rejects_a_payload_it_cannot_trust(tmp_path: Path, payload: str) -> None:
    # Every rejection collapses to None so the caller degrades to import-restore
    # rather than acting on a path it cannot vouch for.
    (tmp_path / RECORD_NAME).write_text(payload, encoding="ascii")
    assert read_trash_origin(tmp_path) is None


def test_read_trash_origin_refuses_an_oversized_file(tmp_path: Path) -> None:
    (tmp_path / RECORD_NAME).write_text(
        json.dumps({"schema": 1, "origin": "/music/A", "moved": "folder", "pad": "x" * 70_000}),
        encoding="ascii",
    )
    assert read_trash_origin(tmp_path) is None


def test_move_back_target_refuses_an_origin_outside_the_library(tmp_path: Path) -> None:
    # A record is a path we are about to shutil.move a folder ONTO. It is also
    # what a re-pointed library looks like, and both answers are the same.
    outside = TrashOrigin(origin="/etc/cron.d", moved="folder", trashed_at=None)
    inside = TrashOrigin(origin=str(tmp_path / "music" / "A"), moved="folder", trashed_at=None)
    music = str(tmp_path / "music")
    assert move_back_target(outside, music_dir=music) is None
    assert move_back_target(inside, music_dir=music) == tmp_path / "music" / "A"


# ----- the listing tells the UI which restore each row gets, and why -----


def test_listing_offers_a_move_back_for_a_recorded_album(tmp_path: Path) -> None:
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    with lib.transaction():
        trash_album_folder(lib, album, trash_dir=tmp_path / "trash")

    (row,) = list_trashed_albums(tmp_path / "trash", music_dir=str(tmp_path / "music"))
    assert row.restore_mode == "move_back"
    assert row.restore_note is None
    assert row.origin == str(tmp_path / "music" / "Portishead" / "Dummy")


def test_listing_marks_a_row_with_no_record_as_an_import(tmp_path: Path) -> None:
    # Owner decision 2: a row that predates the record must SAY so, not silently
    # restore somewhere else.
    trash = tmp_path / "trash"
    _tagged_flac(
        trash / "Portishead - Dummy" / "01 a.flac",
        artist="Portishead",
        album="Dummy",
        title="a",
        track=1,
    )

    (row,) = list_trashed_albums(trash, music_dir=str(tmp_path / "music"))
    assert row.restore_mode == "import"
    assert row.origin is None
    assert row.restore_note is not None
    assert "no record" in row.restore_note


def test_listing_marks_a_shared_folder_row_as_an_import_but_shows_its_origin(
    tmp_path: Path,
) -> None:
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash")

    (row,) = list_trashed_albums(tmp_path / "trash", music_dir=str(tmp_path / "music"))
    assert row.restore_mode == "import"
    assert row.origin == str(tmp_path / "music" / "Portishead" / "Dummy")
    assert row.restore_note is not None
    assert "shared" in row.restore_note


def test_listing_marks_an_origin_outside_the_library_as_an_import(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    entry = trash / "Dummy"
    entry.mkdir(parents=True)
    write_trash_origin(entry, origin=str(tmp_path / "elsewhere" / "Dummy"), moved="folder")

    (row,) = list_trashed_albums(trash, music_dir=str(tmp_path / "music"))
    assert row.restore_mode == "import"
    assert row.origin == str(tmp_path / "elsewhere" / "Dummy")
    assert row.restore_note is not None
    assert "not inside the current music library" in row.restore_note


def test_a_recorded_husk_is_a_zero_track_row_that_can_still_move_back(tmp_path: Path) -> None:
    # The row this whole feature exists for. The UI disables Restore on
    # track_count == 0; that rule must now yield to restore_mode, or the one
    # entry with an exact restore is the one the user cannot restore.
    husk = tmp_path / "music" / "Portishead" / "Dummy"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    trash_folder(husk, trash_dir=tmp_path / "trash")

    (row,) = list_trashed_albums(tmp_path / "trash", music_dir=str(tmp_path / "music"))
    assert row.track_count == 0
    assert row.restore_mode == "move_back"
    assert row.origin == str(husk)


# ----- the move-back restore itself -----


def test_restore_puts_an_album_back_at_its_exact_origin(tmp_path: Path) -> None:
    # The folder name deliberately does NOT match the path template: "Weird
    # Folder" is what beets would never produce, so a restore that lands there
    # can only have used the record. This is the test that pins the in-place
    # import — a move-mode re-import re-files by template instead.
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    source = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))
    assert not source.exists()

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert result.restored is True
    assert result.reason == "restored"
    assert result.album_id is not None
    assert sorted(p.name for p in source.glob("*.flac")) == [
        "01 Mysterons.flac",
        "02 Sour Times.flac",
    ]
    assert not (tmp_path / "music" / "Portishead").exists()  # never re-filed by template
    assert not dest.exists()  # gone from Trash
    assert not (source / RECORD_NAME).exists()  # the sidecar came back and was removed
    restored = _dummy(lib)
    assert {os.path.dirname(os.fsdecode(i.path)) for i in restored.items()} == {str(source)}


def test_restore_recreates_an_artist_folder_that_was_swept_away(tmp_path: Path) -> None:
    # The ordinary sequence, not an edge case: delete an album, then let the
    # orphan sweep take the empty artist folder it left behind. The origin's
    # PARENT is then gone, and a restore that does not recreate it fails.
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    artist_dir = tmp_path / "music" / "Portishead"
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))
    artist_dir.rmdir()
    inode = (dest / "01 Mysterons.flac").stat().st_ino

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert result.restored is True
    assert len(list((artist_dir / "Dummy").glob("*.flac"))) == 2
    # The inode is the oracle for HOW it got there. ``shutil.move`` falls back to
    # copytree + rmtree when ``os.rename`` fails, and a missing parent makes it
    # fail — so without the parent mkdir the album is still restored, but by
    # copying every byte and deleting the source: slow, and with a partial-failure
    # window a rename does not have. Same inode = it was renamed.
    assert (artist_dir / "Dummy" / "01 Mysterons.flac").stat().st_ino == inode


def test_restore_does_not_symlink_the_album_when_the_user_config_asks_for_links(
    tmp_path: Path,
) -> None:
    # beets picks the file operation by falling through move, copy, link,
    # hardlink, reflink in order (importer/stages.py:278-291). The in-place
    # import turns move and copy OFF, so a user config of ``link: yes`` would
    # otherwise take over and symlink the album into the TEMPLATED path — files
    # at the origin, library rows pointing somewhere else.
    config["import"]["link"] = True
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    source = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert result.restored is True
    assert not (tmp_path / "music" / "Portishead").exists()
    assert all(not p.is_symlink() for p in source.glob("*.flac"))
    assert {os.path.dirname(os.fsdecode(i.path)) for i in _dummy(lib).items()} == {str(source)}


def test_in_place_and_move_are_mutually_exclusive_and_leak_no_config(tmp_path: Path) -> None:
    # The raise sits ABOVE the snapshots for the reason the surrounding code
    # spells out: anything assigned before an early exit leaks into the
    # process-global beets config, because the finally that restores it never
    # runs.
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    session = WebImportSession(
        lib,
        None,
        [os.fsencode(str(tmp_path / "music" / "Weird Folder"))],
        None,
        ImportBridge(),
        tmp_path / "trash",
        directive=BankApplyDirective(action="asis"),
    )

    with pytest.raises(ValueError):
        run_import_worker(session, move=True, in_place=True)

    assert config["import"]["copy"].get(bool) is True  # the fixture's value, untouched
    assert config["import"]["move"].get(bool) is False


def test_return_to_trash_refuses_to_bury_the_folder_inside_an_occupied_entry(
    tmp_path: Path,
) -> None:
    # shutil.move onto an EXISTING directory moves the source inside it, so the
    # album would end up one level down under its own name and the Trash row
    # would look restorable while pointing at a wrapper. Refuse instead.
    origin = tmp_path / "music" / "Dummy"
    origin.mkdir(parents=True)
    entry = tmp_path / "trash" / "Dummy"
    entry.mkdir(parents=True)

    with pytest.raises(TrashRestoreIncompleteError):
        _return_to_trash(origin, entry)

    assert origin.is_dir()
    assert list(entry.iterdir()) == []


def test_restore_puts_an_audio_free_husk_back(tmp_path: Path) -> None:
    # Nothing to import, so the move IS the restore. Before the record this
    # folder had no exit from Trash at all.
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    husk = tmp_path / "music" / "Artist Extras"
    husk.mkdir(parents=True)
    (husk / "booklet.jpg").write_bytes(b"\x00")
    dest = trash_folder(husk, trash_dir=tmp_path / "trash")

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert result.restored is True
    assert result.album_id is None
    assert (husk / "booklet.jpg").is_file()
    assert not (husk / RECORD_NAME).exists()
    assert not dest.exists()


def test_an_import_restore_drops_a_record_it_has_outlived(tmp_path: Path) -> None:
    # The shared-folder shape takes the import branch, which moves the files out
    # from under the record. Leaving it behind means a stale origin sitting on an
    # emptied folder — and one that would start offering a move-back again the
    # day the library's ``directory`` moves back.
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash")
    container = next(p for p in (tmp_path / "trash").iterdir() if p.is_dir())
    assert read_trash_origin(container) is not None

    result = restore_album(lib, str(container), trash_dir=tmp_path / "trash")

    assert result.restored is True
    assert read_trash_origin(container) is None


def test_a_failed_import_restore_keeps_the_record(tmp_path: Path) -> None:
    # The other half of the rule above, and the one that costs something if it is
    # wrong: beets SKIPS a duplicate, so the files never leave Trash. Dropping
    # the record there would strip the folder's origin while it is still sitting
    # in Trash — permanently downgrading a row that had one.
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash")
    container = next(p for p in (tmp_path / "trash").iterdir() if p.is_dir())
    replacement = Item(
        album="Dummy", albumartist="Portishead", artist="Portishead", title="Mysterons", track=1
    )
    replacement.path = os.fsencode(str(tmp_path / "music" / "Portishead" / "Dummy" / "01 a.flac"))
    lib.add_album([replacement])

    result = restore_album(lib, str(container), trash_dir=tmp_path / "trash")

    assert result.restored is False
    assert result.reason == "already_in_library"
    assert read_trash_origin(container) is not None


def test_restore_refuses_when_the_origin_is_occupied_and_keeps_the_files_in_trash(
    tmp_path: Path,
) -> None:
    # A restore that lands BESIDE the thing it was meant to be is not a restore,
    # and shutil.move onto an existing directory buries the folder inside it.
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    source = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))
    source.mkdir(parents=True)
    (source / "someone else.flac").write_bytes(b"\x00")

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert result.restored is False
    assert result.reason == "origin_occupied"
    assert len(list(dest.glob("*.flac"))) == 2  # still in Trash, untouched
    assert [p.name for p in source.iterdir()] == ["someone else.flac"]


def test_restore_returns_the_folder_to_trash_when_the_album_is_already_in_the_library(
    tmp_path: Path,
) -> None:
    # The move-back is all-or-nothing: beets skips a duplicate, so the folder
    # goes straight back to Trash with its record intact and the user gets the
    # same answer this endpoint has always given.
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    source = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))
    replacement = Item(
        album="Dummy", albumartist="Portishead", artist="Portishead", title="Mysterons", track=1
    )
    replacement.path = os.fsencode(str(tmp_path / "music" / "Portishead" / "Dummy" / "01 a.flac"))
    lib.add_album([replacement])

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert result.restored is False
    assert result.reason == "already_in_library"
    assert len(list(dest.glob("*.flac"))) == 2  # back in Trash
    assert read_trash_origin(dest) is not None  # with its record
    assert not source.exists()


def test_restore_refuses_to_move_into_an_unavailable_music_share(tmp_path: Path) -> None:
    # The move-back WRITES into the music library, so it answers a dropped share
    # the way delete does. The stronger predicate, not the cheap root check:
    # this is the state where every album looks deleted at once.
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))
    shutil.rmtree(tmp_path / "music")

    with pytest.raises(LibraryRootUnavailableError):
        restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert len(list(dest.glob("*.flac"))) == 2  # nothing left Trash


def test_restore_without_a_record_still_re_imports_as_before(tmp_path: Path) -> None:
    # The control for every "move_back" assertion above, and the guarantee that
    # rows predating the record keep the behaviour they have always had: beets
    # files the album under the CURRENT path template, not at "Weird Folder".
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    trash = tmp_path / "trash"
    _tagged_flac(
        trash / "Weird Folder" / "01 Mysterons.flac",
        artist="Portishead",
        album="Dummy",
        title="Mysterons",
        track=1,
    )

    result = restore_album(lib, str(trash / "Weird Folder"), trash_dir=trash)

    assert result.restored is True
    assert result.album_id is not None
    assert (tmp_path / "music" / "Portishead" / "Dummy").is_dir()
    assert not (tmp_path / "music" / "Weird Folder").exists()
