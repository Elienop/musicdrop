"""The origin record a trashed folder carries, and the move-back restore on it.

Covers the invariant the feature exists for: a folder MusicDrop moves to Trash
must carry enough to be put back exactly where it came from — and recording that
must never be able to make a delete fail.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from beets import config
from beets.library import Album, Item, Library

from app.beets.import_session import ImportBridge, WebImportSession, run_import_worker
from app.beets.library import LibraryRootUnavailableError, _require_id
from app.beets.trash import trash_album, trash_album_folder, trash_folder
from app.beets.trash_manage import (
    TrashRestoreIncompleteError,
    _restore_to_origin,
    _return_to_trash,
    list_trashed_albums,
    restore_album,
)
from app.beets.trash_record import (
    _MAX_ORIGIN_CHARS,
    RECORD_NAME,
    TrashOrigin,
    move_back_target,
    read_trash_origin,
    write_trash_origin,
)
from app.fsutil import exists
from app.models.bank import BankApplyDirective
from app.models.trash import RestoreResult
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
        # An origin is a PATH, not any absolute-looking string. Each of these got
        # through the old `isinstance(str) and isabs()` pair and reached a sink.
        pytest.param(
            json.dumps({"schema": 1, "origin": "/music/A\x00B", "moved": "folder"}),
            id="nul-in-origin",
        ),
        pytest.param(
            json.dumps({"schema": 1, "origin": "/music/A\nB", "moved": "folder"}),
            id="newline-in-origin",
        ),
        pytest.param(
            json.dumps({"schema": 1, "origin": "/music/A\x1b[31mB", "moved": "folder"}),
            id="ansi-escape-in-origin",
        ),
        pytest.param(
            json.dumps({"schema": 1, "origin": "/music/\u202egpm.3pm", "moved": "folder"}),
            id="bidi-override-in-origin",
        ),
        pytest.param(
            json.dumps({"schema": 1, "origin": "/music/" + "A" * 60_000, "moved": "folder"}),
            id="over-long-origin",
        ),
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
    # Both causes, because ``read_trash_origin`` collapses them onto one ``None``:
    # a row that predates the record and a row whose record write FAILED get this
    # same sentence, so naming only the first blames a feature that shipped today
    # for a disk that filled up thirty seconds ago — and nobody looks at the disk.
    assert "no usable record" in row.restore_note
    assert "before origins were recorded" in row.restore_note
    assert "writing that record failed" in row.restore_note
    assert "server log" in row.restore_note


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


# ----- The record write must never follow a planted symlink -----
#
# ``shutil.move`` preserves symlinks, so a trashed folder holds whatever the
# SOURCE folder held. Anyone who can write into the music library — a household
# Samba export, an *arr container on the same media volume, a second user — can
# pre-plant the record name as a link to any file this process can write. The
# delete then truncates it, and because the write SUCCEEDS the swallow-and-log
# arm never fires: silent by construction. Measured before the fix: one album
# delete overwrote beets' config.yaml and truncated library.db 4112 -> 148 bytes.


def _plant_a_symlink_bomb(tmp_path: Path) -> tuple[Path, Path, str]:
    """A staged album whose record name is a link to a file outside the library."""
    album = tmp_path / "music" / "Artist" / "Album"
    album.mkdir(parents=True)
    outside = tmp_path / "data"
    outside.mkdir()
    victim = outside / "config.yaml"
    victim.write_text("directory: /music\nlibrary: /data/beets/library.db\n")

    (album / RECORD_NAME).symlink_to(victim)
    (album / "01 track.mp3").write_bytes(b"\x00" * 16)
    return album, victim, victim.read_text()


def test_write_trash_origin_never_writes_through_a_planted_symlink(tmp_path: Path) -> None:
    """The record replaces the link; it does not write down it.

    Both halves asserted together: the plant really does survive the move (so
    the test is exercising the real shape, not a strawman), and the victim is
    untouched afterwards.
    """
    album, victim, before = _plant_a_symlink_bomb(tmp_path)
    trash = tmp_path / "trash"
    trash.mkdir()
    dest = trash / "Album"
    shutil.move(str(album), str(dest))
    assert (dest / RECORD_NAME).is_symlink(), "the plant must survive the move"

    write_trash_origin(dest, origin=str(album), moved="folder")

    assert victim.read_text() == before, "a file outside the library was overwritten"
    record = dest / RECORD_NAME
    assert record.is_file()
    assert not record.is_symlink()
    assert json.loads(record.read_text())["moved"] == "folder"
    assert [p.name for p in dest.iterdir() if p.name.endswith(".tmp")] == []


def test_trash_folder_does_not_detonate_a_planted_symlink(tmp_path: Path) -> None:
    """End to end through the orphan sweep's mover, not just the primitive.

    ``trash_folder`` is the reorganize sweep's path and takes no library at all,
    so it is the cheapest whole-mover proof that the fix is wired in rather than
    only unit-tested.
    """
    album, victim, before = _plant_a_symlink_bomb(tmp_path)
    trash = tmp_path / "trash"

    dest = trash_folder(album, trash_dir=trash)

    assert victim.read_text() == before, "an album delete overwrote a file outside the library"
    assert read_trash_origin(dest) is not None, "the record still has to be written"


# ----- an ABSENT record and an UNUSABLE one are not the same event -----


def test_a_present_but_unusable_record_is_logged_and_an_absent_one_is_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The return still collapses to ``None``; the LOG is where they separate.

    ``read_trash_origin`` answering ``None`` for both is correct — the caller
    degrades to import-restore either way — but it made a corrupt or unreadable
    record indistinguishable from a folder that simply predates the feature, and
    the Trash row says the same sentence for both. Nothing anywhere pointed at
    the difference, so a record going bad was invisible.

    Both halves in one test on purpose: "the bad one warns" is only worth having
    beside "the ordinary one stays quiet", or a fix that warned on every listing
    of every pre-record folder would pass the first half and flood the log.
    """
    absent = tmp_path / "absent"
    absent.mkdir()
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / RECORD_NAME).write_text("{ not json at all", encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_record"):
        assert read_trash_origin(absent) is None
        assert caplog.records == [], "a folder with no record is the ordinary case"
        assert read_trash_origin(corrupt) is None

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "present but unusable" in record.getMessage()
    assert RECORD_NAME in record.getMessage()


@pytest.mark.parametrize(
    ("plant", "why"),
    [
        pytest.param(lambda p: p.write_text("{ nope", encoding="ascii"), "JSON", id="unparseable"),
        pytest.param(
            lambda p: p.write_text(
                json.dumps({"schema": 1, "origin": "relative", "moved": "folder"}),
                encoding="ascii",
            ),
            "not a record this version can trust",
            id="rejected-payload",
        ),
        pytest.param(
            lambda p: p.write_text(
                json.dumps({"schema": 1, "origin": "/a", "moved": "folder", "pad": "x" * 70_000}),
                encoding="ascii",
            ),
            "cap",
            id="oversized",
        ),
        pytest.param(lambda p: p.mkdir(), "not a regular file", id="a-directory"),
    ],
)
def test_every_unusable_record_names_its_own_cause(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    plant: Callable[[Path], object],
    why: str,
) -> None:
    """Four ways to be unusable, four different sentences.

    One generic "could not read the record" would leave the reader no better off
    than the collapsed ``None`` did: a truncated write, a hand-edited payload, a
    62 KB file and a directory sitting on the name need four different actions.
    """
    plant(tmp_path / RECORD_NAME)

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_record"):
        assert read_trash_origin(tmp_path) is None

    (record,) = caplog.records
    assert why in record.getMessage()


def test_a_row_whose_record_could_not_be_written_no_longer_blames_its_age(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The finding, end to end: the write fails NOW and the row said "before".

    A real write failure, no monkeypatching — a DIRECTORY already sitting at the
    sidecar's name, so ``os.replace`` cannot put the record there. The Trash row
    used to read "it was moved to Trash before origins were recorded", which is
    false and, worse, unfalsifiable: it points at the folder's age instead of at
    the volume that just went read-only, so nobody investigates and every later
    delete loses its origin the same silent way.
    """
    husk = tmp_path / "music" / "Old Name"
    (husk / RECORD_NAME).mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_record"):
        trash_folder(husk, trash_dir=tmp_path / "trash")

    # The write announced its own failure at the time it happened...
    assert any("could not record the Trash origin" in r.getMessage() for r in caplog.records)
    # ...and the row the user reads no longer attributes it to the folder's age.
    (row,) = list_trashed_albums(tmp_path / "trash", music_dir=str(tmp_path / "music"))
    assert row.restore_note is not None
    assert "writing that record failed" in row.restore_note
    assert "server log" in row.restore_note


# ----- an origin is a PATH, and the sinks assume it -----


def test_a_nul_in_the_origin_cannot_reach_the_move(tmp_path: Path) -> None:
    """The whole chain, not just the parse — every link is asserted here.

    A NUL passes ``isabs``; ``app.fsutil.exists`` answers False because
    ``Path.exists()`` swallows the ``ValueError`` internally; so the occupancy
    guard PASSES and the failure lands at the move as a ``ValueError`` that
    ``_restore_to_origin``'s ``except OSError`` does not catch — a blanket 500 on
    the one row the UI had labelled "Exact restore", instead of the documented
    import fallback.

    The two stdlib links are pinned as well as the fix, so a future Python that
    closes either of them is visible here rather than leaving a guard whose
    reason for existing has quietly evaporated.
    """
    entry = tmp_path / "trash" / "Portishead - Dummy"
    entry.mkdir(parents=True)
    poisoned = f"{tmp_path}/music/Portishead/Du\x00mmy"
    (entry / RECORD_NAME).write_text(
        json.dumps({"schema": 1, "origin": poisoned, "moved": "folder"}), encoding="ascii"
    )

    assert exists(Path(poisoned)) is False, "the occupancy guard fails OPEN on a NUL"
    with pytest.raises(ValueError, match="null"):
        os.rename(str(entry), poisoned)  # and the move is where it detonates

    # So the record never becomes a move target in the first place.
    assert read_trash_origin(entry) is None


def test_the_origin_length_cap_sits_at_path_max_and_not_below_it(tmp_path: Path) -> None:
    """Bounded, but not so tightly that a legal deep path loses its exact restore.

    Unbounded, an origin is capped only by the 64 KB FILE limit, so a 60,002-
    character string parses and renders verbatim into the Trash row — the one row
    whose job is telling the user where their files will go. The bound is
    ``PATH_MAX``, and the lower half of this test is what keeps it honest: a
    round number chosen for looks would refuse paths the kernel accepts.
    """
    # Pinned as a LITERAL, and grounded in the kernel beside it. A version of
    # this test that built its inputs from ``_MAX_ORIGIN_CHARS`` moved with the
    # constant and proved only self-consistency: mutating 4096 to 4095 SURVIVED
    # it. 4096 is Linux's PATH_MAX in bytes including the terminating NUL
    # (``linux/limits.h``), and a UTF-8 string never has more characters than
    # bytes, so no path the kernel accepts can exceed it.
    assert _MAX_ORIGIN_CHARS == 4096
    assert _MAX_ORIGIN_CHARS >= os.pathconf("/", "PC_PATH_MAX")
    at_cap = "/" + "a" * 4095
    over_cap = "/" + "a" * 4096

    for origin, expected in ((at_cap, at_cap), (over_cap, None)):
        (tmp_path / RECORD_NAME).write_text(
            json.dumps({"schema": 1, "origin": origin, "moved": "folder"}), encoding="ascii"
        )
        record = read_trash_origin(tmp_path)
        assert (record.origin if record else None) == expected


def test_a_non_utf8_origin_still_gets_its_exact_restore(tmp_path: Path) -> None:
    """The control for the character guard: surrogates are NOT rejected.

    U+DC80-U+DCFF is how a non-UTF-8 POSIX filename survives ``os.fsdecode``, so
    a guard that swept them up with the control characters would deny an exact
    restore to precisely the paths this app takes the most care over — and would
    look identical to a correct one on every test above.
    """
    origin = os.fsdecode(os.fsencode(str(tmp_path / "music")) + b"/Caf\xe9 Album")
    (tmp_path / RECORD_NAME).write_text(
        json.dumps({"schema": 1, "origin": origin, "moved": "folder"}), encoding="ascii"
    )

    record = read_trash_origin(tmp_path)
    assert record is not None
    assert record.origin == origin
    assert move_back_target(record, music_dir=str(tmp_path / "music")) == Path(origin)


# ----- the occupancy guard is a pre-filter; the refusal lives at the syscall -----


def test_a_move_back_refuses_an_origin_that_appeared_in_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's comment promises a refusal; ``shutil.move`` delivered a burial.

    ``exists(origin)`` and the move are two syscalls. Anything that creates the
    origin in between — a sync client, an ``*arr``, the user — made
    ``shutil.move`` treat it as a CONTAINER and put the album INSIDE it, one
    level down under the Trash entry's own name: still complete, still on disk,
    and in a place nothing looks for it.

    ``monkeypatch`` IS the window: the guard is told the origin is free while the
    filesystem knows it is not. That is the only way to land inside a race
    deterministically, and it exercises the real ``os.rename`` underneath.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    origin = tmp_path / "music" / "Portishead" / "Dummy"
    entry = tmp_path / "trash" / "Portishead - Dummy"
    entry.mkdir(parents=True)
    (entry / "01 restored.flac").write_bytes(b"\x00")
    before = sorted(p.name for p in origin.iterdir())

    monkeypatch.setattr("app.beets.trash_manage.exists", lambda _p: False)
    result = _restore_to_origin(lib, entry, origin, trash_dir=tmp_path / "trash")

    assert result == RestoreResult(restored=False, reason="origin_occupied")
    assert not (origin / entry.name).exists(), "the album was buried one level down"
    assert (entry / "01 restored.flac").is_file(), "it must still be in Trash"
    assert sorted(p.name for p in origin.iterdir()) == before


def test_return_to_trash_refuses_an_entry_that_appeared_in_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_return_to_trash`` has the same two-syscall shape and the same burial.

    Its own docstring promises it refuses to move onto an existing entry, and the
    stakes are higher than the forward move's: this runs only when a restore has
    already failed, so burying the folder here is the second thing to go wrong to
    an album that is already in trouble.
    """
    origin = tmp_path / "music" / "Portishead" / "Dummy"
    origin.mkdir(parents=True)
    (origin / "01 a.flac").write_bytes(b"\x00")
    entry = tmp_path / "trash" / "Portishead - Dummy"
    entry.mkdir(parents=True)
    (entry / "stranger.flac").write_bytes(b"\x00")

    # The window: the pre-check is told the Trash entry is free, the disk is not.
    monkeypatch.setattr("app.beets.trash_manage.exists", lambda p: Path(p) != entry)
    with pytest.raises(TrashRestoreIncompleteError):
        _return_to_trash(origin, entry)

    assert not (entry / origin.name).exists(), "the folder was buried inside the entry"
    assert (origin / "01 a.flac").is_file()
    assert sorted(p.name for p in entry.iterdir()) == ["stranger.flac"]


# ----- a Trash folder name reaches the log, and it comes from the album's tags -----


def test_a_failed_return_to_trash_cannot_forge_a_log_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``%r``, not ``%s`` — and the same in the message whose traceback this logs.

    ``_trash_container_name`` neutralises path separators and nothing else, so a
    newline or an ANSI escape in an ``albumartist`` survives into the folder
    name; ``display_path`` replaces only UNDECODABLE bytes, never control
    characters. Interpolated raw, that lets a crafted album name write whatever
    it likes into the server log, on the one code path an operator reads when a
    restore has already gone wrong.

    The HTTP surface was never affected (JSON escapes it), which is exactly why
    this needs its own test: nothing else would have caught it.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    forged = "Dummy\x1b[31m\nCRITICAL:app:all clear"
    origin = tmp_path / "music" / "Portishead" / forged
    entry = tmp_path / "trash" / "Portishead - Dummy"
    entry.mkdir(parents=True)
    (entry / "01 a.flac").write_bytes(b"\x00")

    def _import_destroys_the_folder_then_fails(*_a: object, **_k: object) -> RestoreResult:
        # Leaves the state _return_to_trash refuses to work from, so the failure
        # reaches the log line under test without a second monkeypatch.
        shutil.rmtree(origin)
        raise RuntimeError("the import blew up")

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import", _import_destroys_the_folder_then_fails
    )
    with (
        caplog.at_level(logging.WARNING, logger="app.beets.trash_manage"),
        pytest.raises(RuntimeError),
    ):
        _restore_to_origin(lib, entry, origin, trash_dir=tmp_path / "trash")

    assert any("could not return" in r.getMessage() for r in caplog.records)
    assert "\x1b" not in caplog.text, "an ANSI escape reached the log"
    assert "\nCRITICAL" not in caplog.text, "a forged log line reached the log"
    # Escaped, not dropped: the operator still gets the path they have to look at.
    assert "\\x1b" in caplog.text
    assert "\\n" in caplog.text
