"""The origin recorded for a trashed folder, and the move-back restore on it.

Covers the invariant the feature exists for: a folder MusicDrop moves to Trash
must leave behind enough to be put back exactly where it came from — and
recording that must never be able to make a delete fail.

The record is a SIBLING file (``<origins_dir>/<entry name>.json``), never
anything inside the trashed folder, so the two directories are always built as a
pair here: ``tmp_path/"trash"`` and ``tmp_path/"trash-origins"``. A whole family
of tests that used to live in this file — a planted symlink at the record's
name, a symlinked Trash entry the write escaped through, an oversized file, a
FIFO, a hostile-character denylist — described an attack surface that only
existed because the record sat in a directory arriving from the music library.
They are gone rather than relaxed; see the module docstring of
``app.beets.trash_origins``.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest
from beets import config
from beets.library import Album, Item, Library

from app.beets.import_session import ImportBridge, WebImportSession, run_import_worker
from app.beets.library import LibraryRootUnavailableError, _require_id
from app.beets.trash import (
    resolve_trash_dir,
    resolve_trash_origins_dir,
    trash_album,
    trash_album_folder,
    trash_folder,
)
from app.beets.trash_manage import (
    TrashRestoreIncompleteError,
    _restore_to_origin,
    _return_to_trash,
    empty_all,
    empty_one,
    list_trashed_albums,
    restore_album,
)
from app.beets.trash_origins import (
    _MAX_KEY_BYTES,
    _NAME_MAX,
    TrashOrigin,
    delete_trash_origin,
    move_back_target,
    origin_file,
    origin_recorded,
    read_trash_origin,
    write_trash_origin,
)
from app.config import Settings
from app.fsutil import exists
from app.models.bank import BankApplyDirective
from app.models.trash import RestoreResult
from tests.conftest import build_library, make_test_handle, origins_for

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


def _origins(tmp_path: Path) -> Path:
    """The origin store for this test's Trash dir — its SIBLING, never its child.

    Built through the shared helper so the shape is stated in one place: inside
    ``trash_dir`` a record file would land in the entry namespace ``iterdir``
    walks and list as a trashed album of its own.
    """
    return origins_for(tmp_path / "trash")


def _record(tmp_path: Path, entry: Path) -> TrashOrigin:
    record = read_trash_origin(_origins(tmp_path), entry.name)
    assert record is not None
    return record


# ----- the record is written by every mover that relocates something -----


def _bystander(lib: Library, tmp_path: Path) -> None:
    """One unrelated album really on disk, so the library does not look unmounted.

    A restore writes INTO the music library and so runs behind
    ``require_library_present``. A library with no album anywhere on disk IS the
    dropped-share fixture, so a restore test built on one was asserting through
    a guard that should have refused it. ``_seeded_library`` already carries a
    bystander for exactly this reason; the two tests below build theirs directly.
    """
    dst = tmp_path / "music" / "Bystander" / "Album" / "01 t.flac"
    _tagged_flac(dst, artist="Bystander", album="Album", title="T", track=1)
    item = Item(album="Album", albumartist="Bystander", artist="Bystander", title="T", track=1)
    item.path = os.fsencode(str(dst))
    lib.add_album([item]).store()


def test_trash_album_folder_records_the_folder_it_came_from(tmp_path: Path) -> None:
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    source = str(tmp_path / "music" / "Portishead" / "Dummy")
    album = _dummy(lib)

    with lib.transaction():
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )

    record = _record(tmp_path, dest)
    assert record.origin == source
    assert record.moved == "folder"  # a whole directory moved -> a move-back is exact


def test_trash_folder_records_the_husk_origin(tmp_path: Path) -> None:
    # The husk case is the one with NO other exit from Trash: an audio-free
    # art/booklet folder cannot be imported, so before the record its only
    # remaining option was permanent deletion.
    husk = tmp_path / "music" / "Portishead" / "Dummy"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")

    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    record = _record(tmp_path, dest)
    assert record.origin == str(husk)
    assert record.moved == "folder"


def test_trash_album_records_the_source_folder_as_items(tmp_path: Path) -> None:
    # The per-item mover takes tracked FILES out of a folder that may hold other
    # music, so the origin is recorded for display but a move-back is not on
    # offer — see trash_origins.MovedShape.
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    source = str(tmp_path / "music" / "Portishead" / "Dummy")
    album = _dummy(lib)

    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    container = next(p for p in (tmp_path / "trash").iterdir() if p.is_dir())
    record = _record(tmp_path, container)
    assert record.origin == source
    assert record.moved == "items"
    assert move_back_target(record, music_dir=str(tmp_path / "music")) is None


def test_the_record_carries_a_trash_time_nothing_else_on_disk_keeps(tmp_path: Path) -> None:
    """``trashed_at`` has no reader, and this test is what keeps it on disk.

    It is written for the human who ``cat``s the record. The record file now has
    an mtime of its own, which weakens the old "nothing else on disk keeps this"
    argument — but only weakens it: an mtime does not survive a backup restore, a
    ``cp`` without ``-p`` or an rsync, and the payload does. A field with no
    reader greps as dead code, and the class docstring saying "do not clean it
    up" loses that argument to anyone who greps first; a failing test wins it.
    Without this, deleting the write passes all 2891 tests (measured), and the
    gap would be permanent for every row trashed before anyone noticed.

    Read from the raw JSON on purpose: :class:`TrashOrigin` deliberately does
    NOT surface the field, so going through ``read_trash_origin`` would pin
    nothing.
    """
    husk = tmp_path / "music" / "Portishead" / "Dummy"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    before = datetime.now(UTC)

    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    record_path = origin_file(_origins(tmp_path), dest.name)
    payload = json.loads(record_path.read_text(encoding="ascii"))
    stamped = datetime.fromisoformat(payload["trashed_at"])
    # Offset-aware, or it cannot be read on a machine in another zone later.
    assert stamped.tzinfo is not None
    assert before <= stamped <= datetime.now(UTC)


# ----- recording must never be able to fail a delete -----


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("does-not-exist", id="ascii"),
        pytest.param("Homogenic \u00c9dition", id="accented"),
        pytest.param(os.fsdecode(b"Dummy \xf6"), id="non-utf8"),
    ],
)
def test_write_trash_origin_swallows_a_failing_write(tmp_path: Path, name: str) -> None:
    """The binding secondary invariant, pinned at the contract rather than a caller.

    This is a NEW write on the delete path, so its failure must be no worse than
    today (folder in Trash, no record). TWO of the three callers run
    ``album.remove()`` on the very next line (``trash.py:231`` for the per-item
    mover, ``trash.py:418`` for the whole-folder one), so anything escaping here
    keeps the library rows while the files are already in Trash.

    Parametrised over the NAME because the failure handler interpolates it, and
    an ASCII fixture exercises the swallow without ever exercising the handler's
    own encoding. Covering it here rather than at each call site pins the
    contract once for all three -- including the per-item mover, whose container
    name comes from the album's tags and can be anything at all.

    The failure is REAL, with no monkeypatching: a regular FILE where the origins
    directory belongs, so ``write_atomic_bytes``'s ``mkdir(parents=True,
    exist_ok=True)`` raises. Chosen over ``chmod`` because a suite run as root
    defeats a permission trick and would pass vacuously.
    """
    origins = tmp_path / "trash-origins"
    origins.write_bytes(b"not a directory")

    write_trash_origin(origins, name, origin="/music/A/B", moved="folder")

    assert origins.is_file()  # nothing was written, and nothing escaped
    assert read_trash_origin(origins, name) is None


def test_a_husk_still_reaches_trash_when_the_record_cannot_be_written(tmp_path: Path) -> None:
    # A real failure, no monkeypatching: a regular FILE where the origins dir
    # belongs, so the store's mkdir raises. The delete must still complete.
    husk = tmp_path / "music" / "Old Name"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    _origins(tmp_path).write_bytes(b"not a directory")

    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    assert dest.is_dir()
    assert (dest / "cover.jpg").is_file()
    assert not husk.exists()
    # No record, but the delete happened.
    assert read_trash_origin(_origins(tmp_path), dest.name) is None


@pytest.mark.parametrize(
    "folder",
    [
        pytest.param("Portishead/Dummy", id="ascii"),
        pytest.param("Bj\u00f6rk/Homogenic \u00c9dition", id="accented"),
        pytest.param(os.fsdecode(b"Bjork/Dummy \xf6"), id="non-utf8"),
    ],
)
def test_an_album_still_reaches_trash_when_the_record_cannot_be_written(
    tmp_path: Path, folder: str
) -> None:
    """The record write must not be able to keep the library rows -- at ANY name.

    Parametrised over the FIXTURE NAME rather than over the failure, because the
    name is what the failure handler touches and an ASCII fixture proves only
    that the write was swallowed. This test passed for months on
    ``Portishead/Dummy`` alone while the handler itself raised
    ``UnicodeDecodeError`` on every non-ASCII path: ``backslashreplace`` on an
    ENCODE escapes only what the target codec cannot encode, and UTF-8 encodes
    everything, so the ``.decode("ascii")`` that followed had real bytes to
    choke on. The escape skipped ``album.remove()`` one line later, leaving the
    files in Trash and the rows in the library -- the exact split this whole
    module exists to prevent.

    Both non-ASCII arms are load-bearing, and for DIFFERENT regressions -- an
    accented name is valid UTF-8 that ASCII cannot carry, while a non-UTF-8
    POSIX name arrives as lone surrogates that UTF-8 itself cannot encode.
    Measured against the plausible spellings of this one log argument:

    ======================================  ========  ========
    argument expression                     accented  non-utf8
    ======================================  ========  ========
    ``display_path`` (shipped)              pass      pass
    the bug: utf-8 backslashreplace, ascii  FAILS     pass
    ascii backslashreplace, decode ascii    pass      pass
    strict ``.encode("ascii")``             FAILS     FAILS
    plain ``.encode("utf-8")``              pass      FAILS
    ======================================  ========  ========

    Note the third row: the obvious one-character fix does not raise, so this
    test does not object to it. It is only worse output, not a crash, and a
    test that failed on it would be pinning a preference.

    The undecodable byte must sit in the LEAF, not a parent directory: ``entry``
    is the TRASH destination, named from the album folder's basename, so a
    surrogate in the artist component never reaches the handler at all. A first
    draft put it there and the arm silently proved nothing while still entering
    the handler -- it takes a mutation matrix, not a green run, to tell those
    apart.
    """
    lib = _seeded_library(tmp_path, folder=folder)
    album = _dummy(lib)
    album_id = _require_id(album.id)
    _origins(tmp_path).write_bytes(b"not a directory")

    with lib.transaction():
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )

    assert lib.get_album(album_id) is None  # the delete completed
    assert len(list(dest.glob("*.flac"))) == 2
    assert read_trash_origin(_origins(tmp_path), dest.name) is None


# ----- reading a record is reading untrusted input -----


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json at all", id="unparseable"),
        pytest.param(json.dumps([1, 2, 3]), id="not-an-object"),
        pytest.param(
            json.dumps({"schema": 2, "name": "Dummy", "origin": "/music/A", "moved": "folder"}),
            id="future-schema",
        ),
        pytest.param(
            json.dumps({"schema": 1, "name": "Dummy", "origin": "music/A", "moved": "folder"}),
            id="relative-origin",
        ),
        pytest.param(
            json.dumps({"schema": 1, "name": "Dummy", "origin": "/music/A", "moved": "sideways"}),
            id="unknown-shape",
        ),
        pytest.param(json.dumps({"schema": 1, "name": "Dummy", "moved": "folder"}), id="no-origin"),
        # The one member of the deleted hostile-character filter that is kept.
        # A POSIX path cannot hold a NUL, so only corruption produces this — but
        # ``Path.exists()`` swallows the ValueError it raises and answers False,
        # so the occupancy pre-filter passes and the failure lands at the move as
        # a ValueError no ``except OSError`` catches: a blanket 500 on a row the
        # UI had just labelled "Exact restore".
        pytest.param(
            json.dumps(
                {"schema": 1, "name": "Dummy", "origin": "/music/A\x00B", "moved": "folder"}
            ),
            id="nul-in-origin",
        ),
    ],
)
def test_read_trash_origin_rejects_a_payload_it_cannot_trust(tmp_path: Path, payload: str) -> None:
    # Every rejection collapses to None so the caller degrades to import-restore
    # rather than acting on a path it cannot vouch for. These are CORRUPTION
    # cases, not hostile ones — a truncated ``os.replace``, a hand-edited file, a
    # payload from a version whose meaning has moved — which is why they survive
    # the record's move to a directory nothing but this app writes into.
    origins = tmp_path / "trash-origins"
    origin_file(origins, "Dummy").parent.mkdir(parents=True, exist_ok=True)
    origin_file(origins, "Dummy").write_text(payload, encoding="ascii")
    assert read_trash_origin(origins, "Dummy") is None


def test_move_back_target_refuses_an_origin_outside_the_library(tmp_path: Path) -> None:
    # A record is a path we are about to shutil.move a folder ONTO. It is also
    # what a re-pointed library looks like, and both answers are the same.
    outside = TrashOrigin(origin="/etc/cron.d", moved="folder")
    inside = TrashOrigin(origin=str(tmp_path / "music" / "A"), moved="folder")
    music = str(tmp_path / "music")
    assert move_back_target(outside, music_dir=music) is None
    assert move_back_target(inside, music_dir=music) == tmp_path / "music" / "A"
    # The library ROOT itself is refused separately from "outside the library",
    # and it is the one that costs something: a corrupt or empty origin that
    # normalises to the music root would hand ``_move_no_merge`` the whole
    # library as a move destination.
    assert move_back_target(TrashOrigin(origin=music, moved="folder"), music_dir=music) is None
    assert (
        move_back_target(TrashOrigin(origin=music + "/", moved="folder"), music_dir=music) is None
    )


# ----- the listing tells the UI which restore each row gets, and why -----


def test_listing_offers_a_move_back_for_a_recorded_album(tmp_path: Path) -> None:
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    with lib.transaction():
        trash_album_folder(lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    (row,) = list_trashed_albums(
        tmp_path / "trash", origins_dir=_origins(tmp_path), music_dir=str(tmp_path / "music")
    )
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

    (row,) = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
    assert row.restore_mode == "import"
    assert row.origin is None
    assert row.restore_note is not None
    # All three arms, because ``read_trash_origin`` collapses them onto one
    # ``None``: a row that predates the record, a row whose record write FAILED,
    # and a row whose record is on disk but unusable (a different entry's, or one
    # the store cannot read) get this same sentence. Naming only the first blames
    # a feature that shipped today for a disk that filled up thirty seconds ago —
    # and nobody looks at the disk.
    assert "no usable record" in row.restore_note
    assert "predate origin records" in row.restore_note
    assert "failed to write" in row.restore_note
    assert "unusable now" in row.restore_note
    assert "server log" in row.restore_note


def test_listing_marks_a_shared_folder_row_as_an_import_but_shows_its_origin(
    tmp_path: Path,
) -> None:
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    (row,) = list_trashed_albums(
        tmp_path / "trash", origins_dir=_origins(tmp_path), music_dir=str(tmp_path / "music")
    )
    assert row.restore_mode == "import"
    assert row.origin == str(tmp_path / "music" / "Portishead" / "Dummy")
    assert row.restore_note is not None
    assert "shared" in row.restore_note


def test_listing_marks_an_origin_outside_the_library_as_an_import(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    entry = trash / "Dummy"
    entry.mkdir(parents=True)
    write_trash_origin(
        _origins(tmp_path),
        entry.name,
        origin=str(tmp_path / "elsewhere" / "Dummy"),
        moved="folder",
    )

    (row,) = list_trashed_albums(
        trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
    )
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
    trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    (row,) = list_trashed_albums(
        tmp_path / "trash", origins_dir=_origins(tmp_path), music_dir=str(tmp_path / "music")
    )
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
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )
    assert not source.exists()

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is True
    assert result.reason == "restored"
    assert result.album_id is not None
    assert sorted(p.name for p in source.glob("*.flac")) == [
        "01 Mysterons.flac",
        "02 Sour Times.flac",
    ]
    assert not (tmp_path / "music" / "Portishead").exists()  # never re-filed by template
    assert not dest.exists()  # gone from Trash
    # The entry is gone and its name is free again, so the record must go too —
    # left behind it would be inherited by whatever takes that name next.
    assert read_trash_origin(_origins(tmp_path), dest.name) is None
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
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )
    artist_dir.rmdir()
    inode = (dest / "01 Mysterons.flac").stat().st_ino

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is True
    assert len(list((artist_dir / "Dummy").glob("*.flac"))) == 2
    # The inode is the oracle for HOW it got there. ``shutil.move`` falls back to
    # copytree + rmtree when ``os.rename`` fails, and a missing parent makes it
    # fail — so without the parent mkdir the album is still restored, but by
    # copying every byte and deleting the source: slow, and with a partial-failure
    # window a rename does not have. Same inode = it was renamed.
    assert (artist_dir / "Dummy" / "01 Mysterons.flac").stat().st_ino == inode


@pytest.mark.parametrize("flag", ["link", "hardlink", "reflink"])
def test_restore_never_links_the_album_when_the_user_config_asks_for_links(
    tmp_path: Path, flag: str
) -> None:
    """All THREE link flags, because only ``link`` was ever tested.

    beets picks the file operation by falling through move, copy, link,
    hardlink, reflink in order (importer/stages.py:278-291). The in-place import
    turns move and copy OFF, so whichever of the three the user has on takes
    over and files the album at the TEMPLATED path — files at the origin,
    library rows pointing somewhere else. That is the whole feature defeated,
    and it was pinned for one flag out of three.

    Measured with the forcing removed: ``hardlink: yes`` links the album into
    ``Portishead/Dummy`` and the rows follow it; ``reflink: yes`` raises
    ``ModuleNotFoundError: No module named 'reflink'`` out of the restore and
    takes the request down. The module genuinely is not installed here, and that
    is recorded rather than asserted — an environment that HAS it still fails
    this test in the mutant, because a reflink copy lands at the templated path
    like the other two.

    ``st_nlink`` is the oracle ``is_symlink`` cannot be: a hardlink leaves the
    source a perfectly ordinary regular file with a second name.
    """
    config["import"][flag] = True
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    source = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is True
    assert not (tmp_path / "music" / "Portishead").exists()  # nothing was filed by template
    assert all(not p.is_symlink() for p in source.glob("*.flac"))
    assert all(p.stat().st_nlink == 1 for p in source.glob("*.flac"))
    assert {os.path.dirname(os.fsdecode(i.path)) for i in _dummy(lib).items()} == {str(source)}


def test_in_place_and_move_are_mutually_exclusive_and_leak_no_config(tmp_path: Path) -> None:
    # The raise sits ABOVE the snapshots for the reason the surrounding code
    # spells out: anything assigned before an early exit leaks into the
    # process-global beets config, because the finally that restores it never
    # runs. The three link flags are asserted for the same ordering reason as
    # copy/move — this arm cannot reach the finally at all, so nothing below the
    # raise may have been assigned yet.
    config["import"]["link"] = True
    config["import"]["hardlink"] = True
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
    assert config["import"]["link"].get(bool) is True
    assert config["import"]["hardlink"].get(bool) is True


def test_a_landed_in_place_restore_hands_the_link_flags_back(tmp_path: Path) -> None:
    """The ``finally`` restores link/hardlink/reflink, and only this can prove it.

    ``config["import"]`` is a process-global confuse singleton, so a forced value
    that is never put back holds for the lifetime of the PROCESS: every later
    manual import files by the wrong operation, and the "Effective config" panel
    (which flattens the live global) shows the wrong thing until a restart.

    Nothing catches that incidentally — ``tests/conftest.py``'s autouse
    ``_clear_beets_globals`` scrubs the config between tests, so a leak is
    invisible to every later test. It has to be asserted in the same test that
    triggers the forcing, which is why this is not folded into the exclusion test
    above: that one never reaches the ``finally``.

    All three are set to NON-defaults first, so "they came back" cannot be
    satisfied by a default that happens to match; ``reflink`` is given the string
    ``"auto"`` because it is a bool-OR-string and the snapshot restores it
    verbatim.
    """
    config["import"]["link"] = True
    config["import"]["hardlink"] = True
    config["import"]["reflink"] = "auto"
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is True  # the forcing really ran, so the finally really fired
    assert config["import"]["link"].get(bool) is True
    assert config["import"]["hardlink"].get(bool) is True
    assert config["import"]["reflink"].get() == "auto"


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
    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is True
    assert result.album_id is None
    assert (husk / "booklet.jpg").is_file()
    assert read_trash_origin(_origins(tmp_path), dest.name) is None
    assert not dest.exists()


def test_an_import_restore_drops_a_record_it_has_outlived(tmp_path: Path) -> None:
    # The shared-folder shape takes the import branch, which moves the files out
    # from under the record. Leaving it behind means a stale origin sitting on an
    # emptied folder — and one that would start offering a move-back again the
    # day the library's ``directory`` moves back.
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))
    container = next(p for p in (tmp_path / "trash").iterdir() if p.is_dir())
    assert read_trash_origin(_origins(tmp_path), container.name) is not None

    result = restore_album(
        lib, str(container), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is True
    assert read_trash_origin(_origins(tmp_path), container.name) is None


def test_a_failed_import_restore_keeps_the_record(tmp_path: Path) -> None:
    # The other half of the rule above, and the one that costs something if it is
    # wrong: beets SKIPS a duplicate, so the files never leave Trash. Dropping
    # the record there would strip the folder's origin while it is still sitting
    # in Trash — permanently downgrading a row that had one.
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    album = _dummy(lib)
    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))
    container = next(p for p in (tmp_path / "trash").iterdir() if p.is_dir())
    replacement = Item(
        album="Dummy", albumartist="Portishead", artist="Portishead", title="Mysterons", track=1
    )
    replacement.path = os.fsencode(str(tmp_path / "music" / "Portishead" / "Dummy" / "01 a.flac"))
    lib.add_album([replacement])

    result = restore_album(
        lib, str(container), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is False
    assert result.reason == "already_in_library"
    assert read_trash_origin(_origins(tmp_path), container.name) is not None


def test_restore_refuses_when_the_origin_is_occupied_and_keeps_the_files_in_trash(
    tmp_path: Path,
) -> None:
    # A restore that lands BESIDE the thing it was meant to be is not a restore,
    # and shutil.move onto an existing directory buries the folder inside it.
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    source = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )
    source.mkdir(parents=True)
    (source / "someone else.flac").write_bytes(b"\x00")

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

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
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )
    replacement = Item(
        album="Dummy", albumartist="Portishead", artist="Portishead", title="Mysterons", track=1
    )
    replacement.path = os.fsencode(str(tmp_path / "music" / "Portishead" / "Dummy" / "01 a.flac"))
    lib.add_album([replacement])

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is False
    assert result.reason == "already_in_library"
    assert len(list(dest.glob("*.flac"))) == 2  # back in Trash
    # With its record: the entry is back under its own name, so the record is
    # still TRUE and deleting it would strand the row on import-restore forever.
    assert read_trash_origin(_origins(tmp_path), dest.name) is not None
    assert not source.exists()


def test_a_row_with_NO_record_also_refuses_an_unavailable_music_share(tmp_path: Path) -> None:
    """The guard has to cover BOTH restore arms, and it used to cover one.

    ``restore_album`` branches on the record: a usable one moves the folder
    back, anything else re-imports. Only the move-back arm was behind
    ``require_library_present``, so a row with NO record — every row trashed
    before origins existed, and every row whose record write failed — answered a
    dropped share with ``200 restored``. Measured before the guard moved up:
    beets filed the album onto the bare mountpoint at ``<music>/__/00.flac`` and
    emptied the Trash entry, while the byte-identical move-back row returned
    503 and moved nothing. The share then remounts OVER that path: the files are
    visible nowhere and the library holds a row whose files "vanished".

    Deliberately paired with the move-back case below rather than folded into
    it: what failed here was the ASYMMETRY, and a single-arm test cannot see an
    asymmetry. This one deletes the record to reach the other arm on otherwise
    identical state.
    """
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=trash, origins_dir=origins))
    delete_trash_origin(origins, dest.name)  # a pre-feature row
    assert read_trash_origin(origins, dest.name) is None
    shutil.rmtree(tmp_path / "music")
    (tmp_path / "music").mkdir()  # the mountpoint survives a dropped share

    with pytest.raises(LibraryRootUnavailableError):
        restore_album(lib, str(dest), trash_dir=trash, origins_dir=origins)

    assert len(list(dest.glob("*.flac"))) == 2, "the files must not have left Trash"
    assert list((tmp_path / "music").iterdir()) == [], "nothing may land on the bare mountpoint"


def test_restore_refuses_to_move_into_an_unavailable_music_share(tmp_path: Path) -> None:
    # The move-back WRITES into the music library, so it answers a dropped share
    # the way delete does. The stronger predicate, not the cheap root check:
    # this is the state where every album looks deleted at once.
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    album = _dummy(lib)
    with lib.transaction():
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )
    shutil.rmtree(tmp_path / "music")

    with pytest.raises(LibraryRootUnavailableError):
        restore_album(lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    assert len(list(dest.glob("*.flac"))) == 2  # nothing left Trash


def test_restore_without_a_record_still_re_imports_as_before(tmp_path: Path) -> None:
    # The control for every "move_back" assertion above, and the guarantee that
    # rows predating the record keep the behaviour they have always had: beets
    # files the album under the CURRENT path template, not at "Weird Folder".
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    _bystander(lib, tmp_path)
    trash = tmp_path / "trash"
    _tagged_flac(
        trash / "Weird Folder" / "01 Mysterons.flac",
        artist="Portishead",
        album="Dummy",
        title="Mysterons",
        track=1,
    )

    result = restore_album(
        lib, str(trash / "Weird Folder"), trash_dir=trash, origins_dir=origins_for(trash)
    )

    assert result.restored is True
    assert result.album_id is not None
    assert (tmp_path / "music" / "Portishead" / "Dummy").is_dir()
    assert not (tmp_path / "music" / "Weird Folder").exists()


# ----- a SYMLINKED Trash entry gets no record: both per-row routes refuse it -----


def test_a_symlinked_trash_entry_is_not_promised_an_exact_restore(tmp_path: Path) -> None:
    """The one thing the sidecar's symlink refusal leaves behind, re-decided.

    For a sidecar that refusal was a security guard: ``mkstemp(dir=entry)``
    resolved the link and wrote the record — and, in an earlier version, wrote
    THROUGH a plant at the record's own name — straight out of Trash and into
    ``/data``. Measured then: one album delete overwrote beets' ``config.yaml``
    and truncated ``library.db`` from 4112 bytes to 148. None of that is reachable
    now; the write never touches the trashed folder, so those tests are gone
    rather than relaxed.

    What is left is a PROMISE problem. A symlinked entry is ordinary rather than
    hostile — ``_album_root`` is ``dirname(item.path)``, so an album whose own
    folder is a symlink into another volume lands in Trash still a symlink,
    because ``shutil.move`` preserves them — and ``resolve_trash_child`` refuses
    a child that IS a link, or any path running through one, on
    ``_is_symlinked_entry`` and BEFORE it resolves anything, so both per-row
    routes turn such a row down. Not the RESOLVED containment check, which is a
    weaker predicate here: a link pointing at a SIBLING Trash entry resolves back
    inside Trash and containment alone passed it, which is how the previous tip
    let a per-row Empty remove the other row. A record would make the listing offer "Exact
    restore" on a row whose Restore button 404s. Writing nothing keeps the row
    honest: the listing reads the link itself and answers ``"refused"``, which
    is the contract value for "both per-row routes will turn this down".
    """
    trash, elsewhere = tmp_path / "trash", tmp_path / "elsewhere"
    trash.mkdir()
    elsewhere.mkdir()
    (elsewhere / "cover.jpg").write_bytes(b"\x00")
    (tmp_path / "music").mkdir()
    entry = trash / "Album"
    entry.symlink_to(elsewhere, target_is_directory=True)

    write_trash_origin(_origins(tmp_path), "Real Album", origin="/music/A", moved="folder")
    trash_folder_origin = "/music/Artist/Album"
    from app.beets.trash import _record_origin

    _record_origin(_origins(tmp_path), entry, origin=trash_folder_origin, moved="folder")

    assert read_trash_origin(_origins(tmp_path), entry.name) is None
    (row,) = [
        r
        for r in list_trashed_albums(
            trash, origins_dir=_origins(tmp_path), music_dir=str(tmp_path / "music")
        )
        if r.folder == "Album"
    ]
    assert row.restore_mode == "refused"  # never a move_back it cannot honour


# ----- an ABSENT record and an UNUSABLE one are not the same event -----


def test_a_present_but_unusable_record_is_logged_and_an_absent_one_is_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The return still collapses to ``None``; the LOG is where they separate.

    ``read_trash_origin`` answering ``None`` for both is correct — the caller
    degrades to import-restore either way — but it made a corrupt or unreadable
    record indistinguishable from a folder that simply predates the feature, and
    the Trash row says the same sentence for both. Nothing anywhere pointed at
    the difference, so a record going bad was invisible. On the ``/data`` side
    the WARNING is MORE diagnostic, not less: unusable there means the volume is
    full, read-only, permission-broken or failing — never a plant.

    Both halves in one test on purpose: "the bad one warns" is only worth having
    beside "the ordinary one stays quiet", or a fix that warned on every listing
    of every pre-record folder would pass the first half and flood the log.
    """
    origins = tmp_path / "trash-origins"
    origin_file(origins, "corrupt").parent.mkdir(parents=True)
    origin_file(origins, "corrupt").write_text("{ not json at all", encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        assert read_trash_origin(origins, "absent") is None
        assert caplog.records == [], "an entry with no record is the ordinary case"
        assert read_trash_origin(origins, "corrupt") is None

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "present but unusable" in record.getMessage()
    assert "corrupt.json" in record.getMessage()


@pytest.mark.parametrize(
    ("plant", "why"),
    [
        pytest.param(lambda p: p.write_text("{ nope", encoding="ascii"), "JSON", id="unparseable"),
        pytest.param(
            lambda p: p.write_text(
                json.dumps({"schema": 1, "name": "Dummy", "origin": "relative", "moved": "folder"}),
                encoding="ascii",
            ),
            "not a record this version can trust",
            id="rejected-payload",
        ),
        # ``_names_entry`` ROUTES, it does not judge: a payload that is not an
        # object at all has no ``name`` to disagree with, so it must fall through
        # to ``_parse`` and be called corrupt. Its ``not isinstance(raw, dict)``
        # disjunct exists for exactly this arm — flipped to
        # ``isinstance(raw, dict) and``, this file is logged as "the record for a
        # different Trash entry", which relabels corruption as a key collision
        # and sends an operator hunting a second entry that does not exist.
        pytest.param(
            lambda p: p.write_text(json.dumps([1, 2, 3]), encoding="ascii"),
            "not a record this version can trust",
            id="not-an-object",
        ),
        # No ``is_file()`` preamble survives, so a directory at the name comes
        # back as the OSError it really is rather than a hand-written sentence —
        # the arm is kept to pin that it degrades instead of escaping.
        pytest.param(lambda p: p.mkdir(), "could not be read", id="a-directory"),
    ],
)
def test_every_unusable_record_names_its_own_cause(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    plant: Callable[[Path], object],
    why: str,
) -> None:
    """Four ways to be unusable, and the sentence each one earns.

    One generic "could not read the record" would leave the reader no better off
    than the collapsed ``None`` did: a truncated write, a hand-edited payload and
    an I/O fault need different actions. Two arms share a sentence deliberately —
    a payload that is not an object at all is CORRUPT, not somebody else's
    record — and that sameness is the routing the ``not-an-object`` arm pins.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    plant(origin_file(origins, "Dummy"))

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        assert read_trash_origin(origins, "Dummy") is None

    (record,) = caplog.records
    assert why in record.getMessage()


def test_a_row_whose_record_could_not_be_written_no_longer_blames_its_age(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The finding, end to end: the write fails NOW and the row said "before".

    A real write failure, no monkeypatching — a regular FILE where the origins
    directory belongs, so the store cannot create it. The Trash row used to read
    "it was moved to Trash before origins were recorded" alone, which is false
    and, worse, unfalsifiable: it points at the folder's age instead of at the volume
    that just went read-only, so nobody investigates and every later delete loses
    its origin the same silent way. That second clause is now the LIKELIER of the
    two, because a full or read-only ``/data`` fails every delete's record at
    once rather than one folder's.
    """
    husk = tmp_path / "music" / "Old Name"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    _origins(tmp_path).write_bytes(b"not a directory")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    # The write announced its own failure at the time it happened...
    assert any("could not record the Trash origin" in r.getMessage() for r in caplog.records)
    # ...and the row the user reads no longer attributes it to the folder's age.
    (row,) = list_trashed_albums(
        tmp_path / "trash", origins_dir=_origins(tmp_path), music_dir=str(tmp_path / "music")
    )
    assert row.restore_note is not None
    assert "failed to write" in row.restore_note
    assert "server log" in row.restore_note


# ----- an origin is a PATH, and the sinks assume it -----


def test_a_nul_in_the_origin_cannot_reach_the_move(tmp_path: Path) -> None:
    """The whole chain, not just the parse — every link is asserted here.

    The rest of the old hostile-character denylist is gone: the record lives on
    the trusted side now, and rejecting a control character only cost a
    legitimately-named folder its exact restore. This one member stays because
    its consequence is not cosmetic, and because only CORRUPTION can produce it
    (a POSIX path cannot hold a NUL — it is the terminator).

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
    origins = _origins(tmp_path)
    origins.mkdir()
    origin_file(origins, entry.name).write_text(
        json.dumps({"schema": 1, "name": entry.name, "origin": poisoned, "moved": "folder"}),
        encoding="ascii",
    )

    assert exists(Path(poisoned)) is False, "the occupancy guard fails OPEN on a NUL"
    with pytest.raises(ValueError, match="null"):
        os.rename(str(entry), poisoned)  # and the move is where it detonates

    # So the record never becomes a move target in the first place.
    assert read_trash_origin(origins, entry.name) is None


def test_an_album_named_with_a_no_break_space_keeps_its_exact_restore(tmp_path: Path) -> None:
    """The general round trip, at a name a validator would have been tempted by.

    This used to pin a DECISION -- not to widen ``_REJECTED_IN_ORIGIN`` to the
    characters that misrepresent a path without being control characters (U+200B,
    U+00AD, U+034F, the U+00A0 used here). That filter is gone with the sidecar,
    so there is no longer a decision to pin: the behaviour it protected -- an
    album folder with an exotic character round-trips to an exact restore -- is
    now universally true rather than a carve-out.

    Kept because the end-to-end shape is worth having (seed -> trash ->
    ``move_back_target`` -> restore -> the folder is back at its own name), and
    because the residual it named is still open and still belongs at the DISPLAY
    layer: a row can promise ``/music/Artist/Album`` and move the folder to a
    path that merely looks like it. Escaping non-printing characters where they
    are RENDERED denies no one a restore; rejecting the record denied an exact
    restore to a name that is ordinary in Windows-authored folders.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy\u00a0Deluxe")
    source = tmp_path / "music" / "Portishead" / "Dummy\u00a0Deluxe"
    album = _dummy(lib)

    with lib.transaction():
        dest = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )

    record = _record(tmp_path, dest)
    assert record.origin == str(source)
    assert move_back_target(record, music_dir=str(tmp_path / "music")) == source

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is True
    assert source.is_dir(), "the folder went back to its own name, not a stripped one"
    assert len(list(source.glob("*.flac"))) == 2


def test_a_non_utf8_origin_still_gets_its_exact_restore(tmp_path: Path) -> None:
    """Lone surrogates round-trip: the ``ensure_ascii`` property, at the unit.

    U+DC80-U+DCFF is how a non-UTF-8 POSIX filename survives ``os.fsdecode``, and
    the whole read path has to carry it: the file is written and read as pure
    ASCII precisely so that such an origin survives a sink
    (``write_atomic_text``) that encodes STRICT UTF-8 and would raise on it. The
    end-to-end half is
    ``test_a_non_utf8_folder_name_round_trips_through_the_ascii_record``; this
    is the unit, and it is the one that would still catch a reader switched to
    ``encoding="utf-8"``.
    """
    origin = os.fsdecode(os.fsencode(str(tmp_path / "music")) + b"/Caf\xe9 Album")
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    origin_file(origins, "Dummy").write_text(
        json.dumps({"schema": 1, "name": "Dummy", "origin": origin, "moved": "folder"}),
        encoding="ascii",
    )

    record = read_trash_origin(origins, "Dummy")
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
    result = _restore_to_origin(
        lib, entry, origin, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

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
        # The double failure now propagates as the UNDO's story rather than the
        # import's ``RuntimeError``; the import is still reachable as ``__cause__``.
        pytest.raises(TrashRestoreIncompleteError) as ei,
    ):
        _restore_to_origin(
            lib, entry, origin, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
        )

    assert isinstance(ei.value.__cause__, RuntimeError)
    assert any("could not return" in r.getMessage() for r in caplog.records)
    assert "\x1b" not in caplog.text, "an ANSI escape reached the log"
    assert "\nCRITICAL" not in caplog.text, "a forged log line reached the log"
    # Escaped, not dropped: the operator still gets the path they have to look at.
    assert "\\x1b" in caplog.text
    assert "\\n" in caplog.text


# ----- the double failure: neither in Trash nor in the library -----


def test_a_failed_restore_whose_undo_also_fails_says_where_the_folder_went(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one state the move-back's all-or-nothing promise does not cover.

    Import fails, the return to Trash fails too, and the album is left in the
    MUSIC LIBRARY with no library rows and no Trash row. Propagating the
    import's own exception answered "Restore failed: <beets error>" and sent the
    user to look in Trash, where there is now nothing — the one error whose
    class docstring promises to carry both paths was logged and discarded in the
    one state where that mattered.

    The re-taken Trash entry is what makes the undo fail, deterministically and
    without a permission trick: a sync client, an ``*arr`` or the user putting
    something back at that path is exactly the window ``_return_to_trash``
    refuses to move into.

    That retaken entry is also why the sentence talks about BOTH paths rather
    than "no longer in Trash": ``_whereabouts`` reads the DISK, and the disk has
    something at each path. Nothing can tell a stranger's folder from a
    half-finished copy of ours, so the message says so and the record survives —
    see ``test_the_record_survives_a_trash_entry_that_still_exists``.
    """
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    origin = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    album_id = _require_id(album.id)
    with lib.transaction():
        entry = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )

    def _import_fails_after_something_retakes_the_trash_entry(
        *_a: object, **_k: object
    ) -> RestoreResult:
        entry.mkdir(parents=True)
        (entry / "stranger.flac").write_bytes(b"\x00")
        raise RuntimeError("beets could not read the album")

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import",
        _import_fails_after_something_retakes_the_trash_entry,
    )
    with pytest.raises(TrashRestoreIncompleteError) as ei:
        restore_album(lib, str(entry), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    message = str(ei.value)
    assert f"at the origin '{origin}'" in message, "the path the files are actually at"
    assert f"in Trash at '{entry}'" in message, "and the one they are no longer at"
    assert "BOTH places" in message
    assert "NOT added to the library database" in message
    assert "beets could not read the album" in message, "the import's own cause"
    assert f"Trash entry '{entry}' again" in message, "and the undo's"
    # The state the sentence describes, asserted rather than assumed.
    assert len(list(origin.glob("*.flac"))) == 2
    assert lib.get_album(album_id) is None
    # The record SURVIVES: something is sitting at the Trash entry, and no
    # observation can say whether it is a stranger's folder or a half-removed
    # piece of ours. Deleting it on the guess costs a real row its exact restore
    # forever; keeping it costs a wrong "Exact restore" promise on a row that
    # ``_restore_to_origin`` refuses on the spot, because the origin it names is
    # occupied by this very album. Keyed on the TRASH entry's name, not the
    # stranded folder's.
    assert read_trash_origin(_origins(tmp_path), entry.name) is not None


def test_a_media_album_whose_import_lands_nothing_still_goes_back_to_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_holds_media`` is the whole difference between a husk and a failure.

    The husk branch turns "beets imported nothing" into ``restored=True``,
    because for an audio-free art/booklet folder the move IS the restore. That
    flip has to be gated on the folder really holding no media: with the gate
    open, an album whose import failed is reported RESTORED while it sits in the
    music library un-imported — no library rows, no Trash row, and a UI that has
    just told the user it worked.

    The exact pair of ``test_restore_puts_an_audio_free_husk_back``: same
    ``could_not_restore`` from beets, opposite answers, and only ``_holds_media``
    separates them.

    The stub is what a real failed import leaves — beets looked, landed nothing,
    and touched no file.
    """
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    origin = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    with lib.transaction():
        entry = Path(
            trash_album_folder(
                lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
            )
        )

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import",
        lambda *_a, **_k: RestoreResult(restored=False, reason="could_not_restore"),
    )
    result = restore_album(
        lib, str(entry), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result == RestoreResult(restored=False, reason="could_not_restore")
    assert sorted(p.name for p in entry.glob("*.flac")) == [
        "01 Mysterons.flac",
        "02 Sour Times.flac",
    ]
    assert not origin.exists(), "nothing may be left in the music library"
    assert read_trash_origin(_origins(tmp_path), entry.name) is not None, (
        "and the row keeps its exact restore"
    )


def test_a_part_way_cross_filesystem_move_says_the_folder_may_be_in_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message has to say BOTH — "check both paths" reads as "one of them".

    ``_move_no_merge`` cannot rename across filesystems, so it copies and then
    removes — and a failure mid-copy leaves a partial copy at the origin while
    the whole folder is still in Trash. No undo is attempted there, deliberately,
    which makes the sentence the only thing the user has: it has to say both
    places may hold the folder, and that a retry refuses rather than compounding
    it.

    EXDEV is forced at ``os.rename`` so the real cross-device branch runs.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    origin = tmp_path / "music" / "Restored Here"
    entry = tmp_path / "trash" / "Weird Folder"
    entry.mkdir(parents=True)
    (entry / "01 Mysterons.flac").write_bytes(b"\x00")

    def _across_a_device_boundary(*_a: object, **_k: object) -> NoReturn:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def _half_a_copy(src: Any, dst: Any, **_k: Any) -> NoReturn:
        os.makedirs(dst)
        (Path(dst) / "01 Mysterons.flac").write_bytes(b"\x00")
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "rename", _across_a_device_boundary)
    monkeypatch.setattr(shutil, "copytree", _half_a_copy)

    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _restore_to_origin(
            lib, entry, origin, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
        )

    message = str(ei.value)
    assert str(origin) in message
    assert str(entry) in message
    assert "BOTH places" in message
    assert "retrying cannot make it worse" in message.lower()
    # The claim the sentence makes, measured: it really is in both.
    assert (entry / "01 Mysterons.flac").is_file()
    assert (origin / "01 Mysterons.flac").is_file()


def test_return_to_trash_names_a_vanished_source_rather_than_the_syscall(
    tmp_path: Path,
) -> None:
    """The half of that guard nothing tested, and the only thing it changes.

    ``not exists(origin)`` is a MESSAGE, not a guard: ``_move_no_merge`` on a
    missing source raises ``FileNotFoundError``, an ``OSError`` the arm below
    turns into this same exception type. Only the wording differs — and the
    wording is all the operator has, because this runs only when a restore has
    already gone wrong and the sentence is the last thing pointing at where the
    files went. Dropping the clause replaces it with a bare errno.
    """
    origin = tmp_path / "music" / "Dummy"  # never created: the import consumed it
    entry = tmp_path / "trash" / "Dummy"

    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _return_to_trash(origin, entry)

    message = str(ei.value)
    assert f"nothing at the origin '{origin}'" in message
    assert f"Trash at '{entry}'" in message
    assert "Errno" not in message, "a raw errno is not an answer to this question"


def test_a_dangling_symlink_at_the_origin_keeps_the_files_in_trash(tmp_path: Path) -> None:
    """``exists`` follows links, so the occupancy pre-filter cannot see this one.

    A dangling ``music/X -> /gone`` reads as ABSENT, the move is attempted, and
    ``os.rename`` answers ENOTDIR — which ``_move_no_merge`` normalises to the
    same ``origin_occupied`` the pre-filter would have given. The user is then
    told a path "exists" that ``ls`` shows as broken, which is a wording problem
    and not a data one: nothing moved and the files are still in Trash. Pinned
    because ENOTDIR is what keeps it that way — without it the ``OSError``
    escapes as an incomplete-restore error about a move that never happened.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    origin = tmp_path / "music" / "Weird Folder"
    origin.symlink_to(tmp_path / "gone")
    entry = tmp_path / "trash" / "Weird Folder"
    entry.mkdir(parents=True)
    (entry / "01 a.flac").write_bytes(b"\x00")

    assert exists(origin) is False, "the pre-filter is blind to a dangling link"

    result = _restore_to_origin(
        lib, entry, origin, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result == RestoreResult(restored=False, reason="origin_occupied")
    assert (entry / "01 a.flac").is_file()
    assert origin.is_symlink()
    assert not origin.exists()


def test_a_non_utf8_folder_name_round_trips_through_the_ascii_record(tmp_path: Path) -> None:
    """The exact restore this app takes the most care over, end to end.

    ``ensure_ascii=True`` escapes the lone surrogates ``os.fsdecode`` produces
    for a non-UTF-8 POSIX name into ``\\udcXX``, which is what lets the record be
    written AND read as pure ASCII. Drop it and ``write_atomic_text``'s STRICT
    UTF-8 encode raises ``UnicodeEncodeError`` inside the swallow-and-log arm: no
    record is left at all, the row degrades to an import-restore with the
    no-usable-record note, and the folder never goes back
    to the name it had.

    Three halves now, not two: the KEY is also non-UTF-8 (the Trash entry's own
    name carries the lone surrogate), which the sidecar's fixed filename never
    exercised. The FILE is ASCII with the undecodable byte escaped, and the
    folder comes back at the byte-exact path it left.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    music = tmp_path / "music"
    husk_bytes = os.fsencode(str(music)) + b"/Caf\xe9 Album"
    husk = Path(os.fsdecode(husk_bytes))
    husk.mkdir()
    (husk / "booklet.jpg").write_bytes(b"\x00")

    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    record_path = origin_file(_origins(tmp_path), dest.name)
    assert record_path.is_file(), "the key itself carries the undecodable byte"
    raw = record_path.read_bytes()
    raw.decode("ascii")  # the whole point of encoding="ascii": this cannot be assumed
    assert rb"\udce9" in raw, "the undecodable byte must survive as an escape"
    assert _record(tmp_path, dest).origin == os.fsdecode(husk_bytes)
    # The LISTING has to find it too, and that is a separate way to get this
    # wrong: the store's key must be the RAW on-disk name, never the display
    # form. ``display_path`` replaces the undecodable byte with U+FFFD, which is
    # a different key entirely — the row would silently read as an import with
    # every unit test above still green.
    (row,) = list_trashed_albums(
        tmp_path / "trash", origins_dir=_origins(tmp_path), music_dir=str(music)
    )
    assert row.restore_mode == "move_back"

    result = restore_album(
        lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path)
    )

    assert result.restored is True
    assert (husk / "booklet.jpg").is_file()
    assert not dest.exists()
    assert os.fsencode(str(husk)) == husk_bytes  # byte-exact, not a lookalike


# ----- the store is a SIBLING of the Trash dir, and both resolve the same way -----


def test_the_origin_store_defaults_to_a_sibling_of_the_trash_dir(tmp_path: Path) -> None:
    """Sibling, never child — and that is a correctness rule, not tidiness.

    ``list_trashed_albums`` walks every top-level entry under ``trash_dir`` and
    surfaces each one as a row (``_audio_free_entries``), so a ``trash-origins/``
    directory living inside it would list as a trashed album of its own, be
    offered for Restore and Empty, and be destroyed by Empty-all along with every
    record it holds. Empty settings resolve BOTH under the handle's already-
    absolute ``beets_dir``, which sidesteps the cwd-relative gotcha.
    """
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    handle = make_test_handle(lib, tmp_path)
    settings = Settings(trash_dir="", trash_origins_dir="")

    trash = resolve_trash_dir(settings, handle)
    origins = resolve_trash_origins_dir(settings, handle)

    assert trash == tmp_path / "trash"
    assert origins == tmp_path / "trash-origins"
    assert origins.parent == trash.parent
    assert not origins.is_relative_to(trash)
    assert not trash.is_relative_to(origins)


def test_a_configured_origins_dir_overrides_the_default(tmp_path: Path) -> None:
    """The override exists so the store can be moved OFF a volume, independently.

    Deliberately not derived from ``trash_dir``: a user-configured Trash dir may
    point anywhere, including inside the music library, and a record reachable
    from ``/music`` is the whole thing this store exists to avoid.
    """
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    handle = make_test_handle(lib, tmp_path)
    elsewhere = tmp_path / "elsewhere" / "origins"

    resolved = resolve_trash_origins_dir(
        Settings(trash_dir=str(tmp_path / "music" / ".trash"), trash_origins_dir=str(elsewhere)),
        handle,
    )

    assert resolved == elsewhere


# ----- every exit from Trash takes the record with it -----


def test_empty_one_removes_the_origin_record(tmp_path: Path) -> None:
    """Invariant 5b, which the sidecar got for free from ``rmtree``.

    Keyed on the entry NAME in a directory of its own, this is a rule instead:
    a record outliving its entry is litter at best, and the name it holds is one
    the allocator will then refuse to hand out again.
    """
    husk = tmp_path / "music" / "Old Name"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))
    assert read_trash_origin(_origins(tmp_path), dest.name) is not None

    assert empty_one(str(dest), origins_dir=_origins(tmp_path)).removed == 1

    assert not dest.exists()
    assert read_trash_origin(_origins(tmp_path), dest.name) is None


def test_empty_one_keeps_the_record_when_the_removal_itself_fails(tmp_path: Path) -> None:
    """Ordering, pinned: the entry goes FIRST, the record only after.

    A failed ``rmtree`` leaves the folder sitting in Trash. Dropping its record
    on the way past would permanently downgrade a row that still exists to an
    import-restore — the one thing this feature must never do.

    Skipped as root, where the mode bit denies nothing and the ``rmtree`` simply
    succeeds. (Its sibling above uses a real FILE where the origins dir belongs
    for exactly that reason; there is no equivalent trick for "``rmtree`` must
    fail", so this one takes the guard.)
    """
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    husk = tmp_path / "music" / "Old Name"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))
    (tmp_path / "trash").chmod(0o500)  # rmtree cannot unlink out of a read-only dir

    try:
        with pytest.raises(OSError):
            empty_one(str(dest), origins_dir=_origins(tmp_path))
    finally:
        (tmp_path / "trash").chmod(0o700)

    assert dest.is_dir()
    assert read_trash_origin(_origins(tmp_path), dest.name) is not None


def test_empty_all_removes_every_origin_record_including_a_symlinked_entry(
    tmp_path: Path,
) -> None:
    """Per child, inside the loop — and the symlinked leaf is not an exception.

    ``empty_all``'s symlink branch unlinks the link without following it; its
    record (which only a hand edit or an older version could have written, since
    ``_record_origin`` declines a symlinked entry) has to go with it, or the name
    stays burnt for good.
    """
    trash, origins = tmp_path / "trash", _origins(tmp_path)
    for name in ("A", "B"):
        husk = tmp_path / "music" / name
        husk.mkdir(parents=True)
        (husk / "cover.jpg").write_bytes(b"\x00")
        trash_folder(husk, trash_dir=trash, origins_dir=origins)
    (tmp_path / "linked").mkdir()
    (trash / "C").symlink_to(tmp_path / "linked", target_is_directory=True)
    write_trash_origin(origins, "C", origin=str(tmp_path / "music" / "C"), moved="folder")

    assert empty_all(trash, origins_dir=origins).removed == 3

    assert list(trash.iterdir()) == []
    assert (tmp_path / "linked").is_dir(), "the link was unlinked, not followed"
    assert sorted(p.name for p in origins.glob("*.json")) == []


def test_empty_all_leaves_a_record_whose_entry_was_removed_outside_the_app(
    tmp_path: Path,
) -> None:
    """The accepted residual, pinned so it is a decision rather than a surprise.

    A hand ``rm -rf`` of a Trash entry leaves its record behind, and no per-row
    action reaches it. The tempting sweep — "unlink every record with no
    matching entry", asked on the LISTING — is still refused: it cannot tell an
    empty Trash dir from a Trash dir whose share has just dropped, and would
    destroy every remaining origin in that state.

    What does reap such a record is an Empty all that empties Trash
    (``clear_trash_origins``, gated on having removed at least one entry), so it
    waits for one rather than surviving for good. THIS case is the wait: Trash
    holds nothing, so ``empty_all`` removes nothing, the gate never fires and the
    record is still there afterwards. Until then the orphan is harmless because
    ``_unique_trash_dest`` treats a recorded name as occupied — it costs a burnt
    name, never a wrong restore.
    """
    trash, origins = tmp_path / "trash", _origins(tmp_path)
    trash.mkdir()
    write_trash_origin(
        origins, "Gone By Hand", origin=str(tmp_path / "music" / "X"), moved="folder"
    )

    assert empty_all(trash, origins_dir=origins).removed == 0

    assert read_trash_origin(origins, "Gone By Hand") is not None


# ----- the name key: what makes it safe, and what it refuses -----


def test_a_name_whose_record_survives_is_never_handed_to_another_folder(tmp_path: Path) -> None:
    """The name-key analogue of the inode-reuse hazard, closed at the allocator.

    An entry deleted outside MusicDrop leaves its record. Nothing in Trash
    answers to that name any more, so without this the next folder to earn it
    would inherit a stale origin — and that origin steers a ``rename()`` into the
    music library for the WRONG folder. Strictly worse than losing an origin,
    which is exactly why inode keys were rejected; the answer is that a recorded
    name is not free.
    """
    origins = _origins(tmp_path)
    stale = str(tmp_path / "music" / "somewhere else entirely")
    write_trash_origin(origins, "Old Name", origin=stale, moved="folder")
    husk = tmp_path / "music" / "Old Name"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")

    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=origins)

    assert dest.name == "Old Name (1)", "the recorded name was still taken"
    assert _record(tmp_path, dest).origin == str(husk)
    stale_record = read_trash_origin(origins, "Old Name")
    assert stale_record is not None
    assert stale_record.origin == stale, "and the stranger's record was not overwritten"


def test_the_key_must_be_a_single_path_component(tmp_path: Path) -> None:
    """Traversal is unreachable today, and the guarantee is incidental.

    ``_trash_container_name`` replaces both separators and
    ``_unique_trash_dest``'s collision loop can never settle on ``.`` or ``..``
    (both always exist) — but that is two functions agreeing by accident, and
    this key names a file on the ``/data`` side beside ``library.db``. Same
    reason ``app/bank/store.py`` keeps ``_VALID_ID``. All three public entry
    points must refuse rather than escape, and none of them may raise at a
    caller that cannot handle it.
    """
    origins = tmp_path / "trash-origins"
    for key in ("", ".", "..", "../escape", "a/b"):
        with pytest.raises(ValueError):
            origin_file(origins, key)
        # Every PUBLIC entry point degrades instead: the two on the delete path
        # are contractually never-raising (a delete must not fail because its
        # bookkeeping did), and the allocator must not blow up choosing a name.
        assert read_trash_origin(origins, key) is None
        write_trash_origin(origins, key, origin="/music/A", moved="folder")  # swallowed
        delete_trash_origin(origins, key)  # swallowed
        assert origin_recorded(origins, key) is False
    assert not (tmp_path / "escape.json").exists()
    assert not (tmp_path / "trash-origins.json").exists()


def test_deleting_a_record_that_was_never_written_is_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``missing_ok=True``, and the SILENCE is the point rather than the no-raise.

    Every Trash row that predates the feature, and every row whose record write
    failed, reaches ``delete_trash_origin`` with nothing to delete — once per
    entry on ``empty_all``, and on every import-restore. Raising there is caught
    by the handler either way, so the cost of getting this wrong is not a crash:
    it is a WARNING per absent record, which buries the warnings that mean
    something (a record present but unusable is how a failing ``/data`` volume
    announces itself).
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, "never written")
        delete_trash_origin(tmp_path / "no-such-dir", "never written")

    assert caplog.records == []


def test_an_entry_name_too_long_for_a_json_suffix_still_gets_its_record(tmp_path: Path) -> None:
    """The longest album folders must not silently lose their exact restore.

    ``NAME_MAX`` is 255 bytes, so ``<entry>.json`` is unwritable for any entry
    within 5 bytes of the limit: the Trash entry creates fine and its record
    cannot, forever (a retry can never succeed). The failure would be swallowed
    and the row would degrade to an import-restore with no hint why.

    The real budget is TIGHTER than ``NAME_MAX`` and this test is what found
    that: the shared atomic writer creates ``.<name>.<pid>.<16 hex>.tmp`` beside
    the target, so a record filename truncated to exactly 255 bytes still raised
    ENAMETOOLONG on the TEMP file and the record was lost with a perfectly legal
    target name. Hence :data:`_MAX_KEY_BYTES`.

    The bound is pinned in BOTH directions, because a test that derived its
    inputs from the constant would move with it and prove only self-consistency:
    the literal catches a value lowered below what the app assumes, and the
    ``pathconf`` floor catches one raised above what the filesystem accepts.
    """
    assert _NAME_MAX == 255
    assert _NAME_MAX <= os.pathconf(str(tmp_path), "PC_NAME_MAX")
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    name = "L" * 251  # 251 + len(".json") = 256

    with pytest.raises(OSError):  # the naive spelling really is unwritable
        (origins / f"{name}.json").write_text("x")

    record_path = origin_file(origins, name)
    assert len(os.fsencode(record_path.name)) <= _MAX_KEY_BYTES
    write_trash_origin(origins, name, origin="/music/A", moved="folder")

    record = read_trash_origin(origins, name)
    assert record is not None, "a long entry name must still earn its origin"
    assert record.origin == "/music/A"
    assert read_trash_origin(origins, "L" * 250) is None, "and the key is not truncated to a stem"
    # End to end, so the whole mover chain is proved and not just the store: an
    # entry name at the filesystem's own limit still earns an exact restore.
    husk = tmp_path / "music" / ("H" * 255)
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    dest = trash_folder(husk, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))
    assert _record(tmp_path, dest).origin == str(husk)


# ----- a mover that relocated nothing must record nothing -----


def test_a_ghost_album_leaves_no_record_for_a_container_that_was_removed(
    tmp_path: Path,
) -> None:
    """The ghost arm returns normally having moved nothing, and ``rmdir``s the
    container it made. A record written then would name an entry that does not
    exist — an orphan manufactured on purpose, and one whose name the allocator
    would then refuse to a real album.

    This was masked while the record lived inside the container: the write failed
    because the directory was gone. On the ``/data`` side it would succeed.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    shutil.rmtree(tmp_path / "music" / "Portishead" / "Dummy")  # the files are genuinely gone
    album = _dummy(lib)
    album_id = _require_id(album.id)

    with lib.transaction():
        trash_album(lib, album, trash_dir=tmp_path / "trash", origins_dir=_origins(tmp_path))

    assert lib.get_album(album_id) is None, "the ghost's rows are dropped — that is the point"
    assert list((tmp_path / "trash").iterdir()) == [], "nothing was moved, so there is no entry"
    assert sorted(p.name for p in _origins(tmp_path).glob("*.json")) == []


def test_a_landed_restore_drops_the_record_keyed_on_the_TRASH_name(tmp_path: Path) -> None:
    """The key is the TRASH entry's name, never the folder's own basename.

    They are the same string in the ordinary case, which is why every other test
    here passes either way. A collision suffix separates them: the entry is
    ``Weird Folder (1)`` while the origin's basename is ``Weird Folder``. Keying
    the clean-up on the folder in front of you — which is what the sidecar
    version effectively did, deleting a file INSIDE the restored folder — then
    leaves the real record behind AND unlinks a stranger's.

    The decoy is not decoration: it is the record of the entry that already owns
    ``Weird Folder``, and it must be untouched afterwards.
    """
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    origins = _origins(tmp_path)
    decoy_origin = str(tmp_path / "music" / "Some Other Album")
    write_trash_origin(origins, "Weird Folder", origin=decoy_origin, moved="folder")
    (tmp_path / "trash" / "Weird Folder").mkdir(parents=True)
    album = _dummy(lib)

    with lib.transaction():
        dest = Path(
            trash_album_folder(lib, album, trash_dir=tmp_path / "trash", origins_dir=origins)
        )
    assert dest.name == "Weird Folder (1)", "the names must really differ for this to test anything"

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash", origins_dir=origins)

    assert result.restored is True
    assert read_trash_origin(origins, dest.name) is None, "the entry's own record must go"
    survivor = read_trash_origin(origins, "Weird Folder")
    assert survivor is not None, "a stranger's record must not be unlinked"
    assert survivor.origin == decoy_origin


def test_the_row_shows_a_normalised_origin(tmp_path: Path) -> None:
    """``normpath`` in the parse, which nothing else would notice.

    Every origin this app WRITES is already normalised (``os.path.abspath`` or
    ``dirname`` of one), so only a hand-edited or corrupted record can carry
    ``/a/./b``. ``move_back_target`` normalises again before it moves anything,
    so the move is safe either way — what this pins is the TEXT, which is the
    field whose entire job is telling the user where their files will go.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    entry = tmp_path / "trash" / "Dummy"
    entry.mkdir(parents=True)
    origin_file(origins, "Dummy").write_text(
        json.dumps(
            {
                "schema": 1,
                "name": "Dummy",
                "origin": f"{tmp_path}/music/./Portishead//Dummy",
                "moved": "folder",
            }
        ),
        encoding="ascii",
    )

    (row,) = list_trashed_albums(
        tmp_path / "trash", origins_dir=origins, music_dir=str(tmp_path / "music")
    )

    assert row.origin == str(tmp_path / "music" / "Portishead" / "Dummy")


def test_an_unusable_record_cannot_forge_a_log_line_through_its_own_filename(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``%r`` on the record path, and it got MORE load-bearing with the move.

    The key is the Trash entry's NAME, which comes from the album's own tags —
    ``_trash_container_name`` neutralises path separators and nothing else — so a
    newline or an ANSI escape in an ``albumartist`` now reaches this log line
    inside the record's own FILENAME. Interpolated with ``%s`` that lets a
    crafted album name write whatever it likes into the server log, on the one
    line an operator reads when a record has gone bad. The sidecar's fixed
    filename could not carry any of this.
    """
    forged = "Dummy\x1b[31m\nCRITICAL:app:all clear"
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    origin_file(origins, forged).write_text("{ not json", encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        assert read_trash_origin(origins, forged) is None

    assert "\x1b" not in caplog.text, "an ANSI escape reached the log"
    assert "\nCRITICAL" not in caplog.text, "a forged log line reached the log"
    # Escaped, not dropped: the operator still gets the path they have to look at.
    assert "\\x1b" in caplog.text
    assert "\\n" in caplog.text


def test_a_name_that_only_the_temp_file_overflows_still_gets_its_record(tmp_path: Path) -> None:
    """The band between the two limits, which is where this is easy to get wrong.

    A 240-byte entry name makes a 245-byte ``<name>.json`` — a filename the
    kernel accepts, so a threshold of ``NAME_MAX`` looks correct and every test
    at 251+ bytes still passes. The ATOMIC write then fails anyway, because
    ``write_atomic_bytes`` puts ``.<name>.<pid>.<16 hex>.tmp`` beside the target,
    and the record is silently lost with a perfectly legal target name.

    Both halves: the plain spelling really would be accepted as a filename (so
    the threshold cannot be justified by ``NAME_MAX``), and the record really
    lands.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    name = "M" * 240
    (origins / f"{name}.probe").write_text("x")  # the kernel accepts this length

    write_trash_origin(origins, name, origin="/music/A", moved="folder")

    record = read_trash_origin(origins, name)
    assert record is not None, "the temp file overflowed and the record was lost"
    assert record.origin == "/music/A"
    assert len(os.fsencode(origin_file(origins, name).name)) <= _MAX_KEY_BYTES


def test_an_import_restore_drops_an_UNREADABLE_record_too(tmp_path: Path) -> None:
    """The clean-up is gated on ``result.restored`` ALONE, not on a usable record.

    ``read_trash_origin`` collapses "no file" and "a file we cannot trust" onto
    the same ``None``, so a corrupt record takes the import branch — and gating
    the delete on ``record is not None`` (which is what the sidecar version did,
    harmlessly, because the file rode out of Trash inside the folder) leaves that
    file behind forever on the ``/data`` side. It is exactly the record a later
    entry of the same name would inherit: unreadable, so it steers nothing, but
    it burns the name for good because the allocator treats it as occupied.
    """
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    _bystander(lib, tmp_path)
    trash, origins = tmp_path / "trash", _origins(tmp_path)
    _tagged_flac(
        trash / "Weird Folder" / "01 Mysterons.flac",
        artist="Portishead",
        album="Dummy",
        title="Mysterons",
        track=1,
    )
    origins.mkdir()
    origin_file(origins, "Weird Folder").write_text("{ not json at all", encoding="ascii")
    assert read_trash_origin(origins, "Weird Folder") is None  # unusable, not absent

    result = restore_album(lib, str(trash / "Weird Folder"), trash_dir=trash, origins_dir=origins)

    assert result.restored is True
    assert not origin_file(origins, "Weird Folder").exists(), "the unusable record must go too"
