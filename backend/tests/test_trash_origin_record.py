"""The origin record a trashed folder carries, and the move-back restore on it.

Covers the invariant the feature exists for: a folder MusicDrop moves to Trash
must carry enough to be put back exactly where it came from — and recording that
must never be able to make a delete fail.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
import signal
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

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


@contextlib.contextmanager
def _deadline(seconds: float) -> Iterator[None]:
    """Turn a HANG into a failure, so a missing guard cannot pass as a green run.

    ``SIGALRM`` rather than a thread or a plugin: it is the only thing that
    interrupts a blocking read on a FIFO. PEP 475 retries an interrupted syscall
    unless the handler raises, so the handler raises.
    """

    def _fire(_signum: int, _frame: FrameType | None) -> NoReturn:
        raise TimeoutError(f"blocked for more than {seconds}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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


def test_the_record_carries_a_trash_time_nothing_else_on_disk_keeps(tmp_path: Path) -> None:
    """``trashed_at`` has no reader, and this test is what keeps it on disk.

    It is written for the human who ``cat``s the sidecar, and because the move is
    a rename — which preserves the folder's OWN mtime — so once a folder is in
    Trash this file is the only place the time it got there survives. A field
    with no reader greps as dead code, and the class docstring saying "do not
    clean it up" loses that argument to anyone who greps first; a failing test
    wins it. Without this, deleting the write passes all 2891 tests (measured),
    and the gap would be permanent for every row trashed before anyone noticed.

    Read from the raw JSON on purpose: :class:`TrashOrigin` deliberately does
    NOT surface the field, so going through ``read_trash_origin`` would pin
    nothing.
    """
    husk = tmp_path / "music" / "Portishead" / "Dummy"
    husk.mkdir(parents=True)
    (husk / "cover.jpg").write_bytes(b"\x00")
    before = datetime.now(UTC)

    dest = trash_folder(husk, trash_dir=tmp_path / "trash")

    payload = json.loads((dest / RECORD_NAME).read_text(encoding="ascii"))
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
    """
    entry = tmp_path / name
    write_trash_origin(entry, origin="/music/A/B", moved="folder")
    assert not entry.exists()


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
    source = tmp_path / "music" / folder
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
    outside = TrashOrigin(origin="/etc/cron.d", moved="folder")
    inside = TrashOrigin(origin=str(tmp_path / "music" / "A"), moved="folder")
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
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

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
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

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


def test_the_record_is_not_written_through_a_symlinked_trash_entry(tmp_path: Path) -> None:
    """The plant one level up: the ENTRY is the link, not the record name.

    ``mkstemp``'s ``O_CREAT|O_EXCL`` -- the fix for the test above -- protects
    the FINAL component only. ``dir=`` is resolved normally, so if the Trash
    entry itself is a symlink both the temp file and the ``os.replace`` land in
    whatever it points at. Measured before this was fixed: the record was
    written into the directory beside beets' ``config.yaml``.

    Nothing hostile is required, which is why this is a real case and not a lab
    one: ``_album_root`` is ``dirname(item.path)``, so an album whose own folder
    is a symlink into another volume arrives in Trash still a symlink, because
    ``shutil.move`` preserves them.

    The right answer is to decline, not to fail: the folder is in Trash either
    way and stays restorable by re-import. Impact was litter at a fixed hidden
    name rather than an overwrite -- but "the write touches only Trash" is an
    invariant this module states, so it has to be true rather than nearly true.
    """
    trash, elsewhere = tmp_path / "trash", tmp_path / "data"
    trash.mkdir()
    elsewhere.mkdir()
    (elsewhere / "config.yaml").write_text("directory: /music\n")
    entry = trash / "Album"
    entry.symlink_to(elsewhere, target_is_directory=True)

    write_trash_origin(entry, origin="/music/Artist/Album", moved="folder")

    assert not (elsewhere / RECORD_NAME).exists(), "the record escaped Trash"
    assert [p.name for p in elsewhere.iterdir()] == ["config.yaml"]
    assert read_trash_origin(entry) is None  # declined, so the row degrades to a re-import


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


def test_an_album_named_with_a_no_break_space_keeps_its_exact_restore(tmp_path: Path) -> None:
    """Pins the DECISION not to widen the origin denylist, and states its cost.

    ``_REJECTED_IN_ORIGIN`` refuses C0/C1/DEL and the Trojan-Source bidi set, and
    nothing else. Several characters that plainly misrepresent a path are absent
    -- U+200B, U+00AD, U+034F and the U+00A0 used here all pass, parse, earn a
    ``move_back_target`` and render into the row intact, so a row can promise
    ``/music/Artist/Album`` and move the folder to a path that merely looks like
    it. That is left open on purpose, and this test is what stops it being
    "fixed" by widening the pattern.

    The trade is asymmetric. The consequence of leaving it is bounded by
    ``move_back_target``'s containment -- a folder somewhere inside the user's
    own library under an unexpected name, confusing rather than a capability.
    The consequence of closing it here is that an album folder GENUINELY named
    with a no-break space, which is ordinary in Windows-authored names, loses its
    exact restore permanently and can never get it back. A real loss traded
    against a cosmetic one.

    Written as the legitimate case rather than as ``assert not
    _REJECTED_IN_ORIGIN.search(...)`` on purpose: the reason is what must survive,
    and asserting the regex would pin the mechanism while saying nothing about
    why. If this ever is closed, the display is the place -- escaping
    non-printing characters denies no one a restore.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy\u00a0Deluxe")
    source = tmp_path / "music" / "Portishead" / "Dummy\u00a0Deluxe"
    album = _dummy(lib)

    with lib.transaction():
        dest = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))

    record = _record(dest)
    assert record.origin == str(source)
    assert move_back_target(record, music_dir=str(tmp_path / "music")) == source

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert result.restored is True
    assert source.is_dir(), "the folder went back to its own name, not a stripped one"
    assert len(list(source.glob("*.flac"))) == 2


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
        # The double failure now propagates as the UNDO's story rather than the
        # import's ``RuntimeError``; the import is still reachable as ``__cause__``.
        pytest.raises(TrashRestoreIncompleteError) as ei,
    ):
        _restore_to_origin(lib, entry, origin, trash_dir=tmp_path / "trash")

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
    """
    lib = _seeded_library(tmp_path, folder="Weird Folder")
    origin = tmp_path / "music" / "Weird Folder"
    album = _dummy(lib)
    album_id = _require_id(album.id)
    with lib.transaction():
        entry = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))

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
        restore_album(lib, str(entry), trash_dir=tmp_path / "trash")

    message = str(ei.value)
    assert str(origin) in message, "the path the files are actually at"
    assert str(entry) in message, "and the one they are no longer at"
    assert "no longer in Trash" in message
    assert "NOT added to the library database" in message
    assert "beets could not read the album" in message, "the import's own cause"
    assert "the source is gone or the Trash entry is occupied" in message, "and the undo's"
    # The state the sentence describes, asserted rather than assumed.
    assert len(list(origin.glob("*.flac"))) == 2
    assert lib.get_album(album_id) is None
    # The record has nothing left to say and would outlive the folder: it names
    # the folder it is now inside, and it would keep the source dir alive
    # through the re-import the message asks for.
    assert not (origin / RECORD_NAME).exists()


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
        entry = Path(trash_album_folder(lib, album, trash_dir=tmp_path / "trash"))

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import",
        lambda *_a, **_k: RestoreResult(restored=False, reason="could_not_restore"),
    )
    result = restore_album(lib, str(entry), trash_dir=tmp_path / "trash")

    assert result == RestoreResult(restored=False, reason="could_not_restore")
    assert sorted(p.name for p in entry.glob("*.flac")) == [
        "01 Mysterons.flac",
        "02 Sour Times.flac",
    ]
    assert not origin.exists(), "nothing may be left in the music library"
    assert read_trash_origin(entry) is not None, "and the row keeps its exact restore"


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
        _restore_to_origin(lib, entry, origin, trash_dir=tmp_path / "trash")

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
    assert "the source is gone or the Trash entry is occupied" in message
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

    result = _restore_to_origin(lib, entry, origin, trash_dir=tmp_path / "trash")

    assert result == RestoreResult(restored=False, reason="origin_occupied")
    assert (entry / "01 a.flac").is_file()
    assert origin.is_symlink()
    assert not origin.exists()


# ----- a record file is untrusted INPUT, and reading one must not block -----


def test_a_fifo_at_the_record_name_cannot_hang_the_listing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``is_file()`` is the only thing between a planted FIFO and a hung request.

    ``GET /api/trash`` is ungated and reads one record per top-level entry on a
    threadpool worker. A FIFO passes the size cap — ``st_size`` is 0 on a pipe
    with no writer — and ``read_text`` then blocks FOREVER, holding that worker.
    Anyone who can create a file in the music library can plant one, and
    ``shutil.move`` carries it into Trash with the folder.

    The deadline is the oracle, and it has to be: without the guard this test
    does not fail, it hangs — which reads as a suite that is still running.
    """
    os.mkfifo(tmp_path / RECORD_NAME)

    with _deadline(5), caplog.at_level(logging.WARNING, logger="app.beets.trash_record"):
        assert read_trash_origin(tmp_path) is None

    (record,) = caplog.records
    assert "not a regular file" in record.getMessage()


def test_a_non_utf8_folder_name_round_trips_through_the_ascii_sidecar(tmp_path: Path) -> None:
    """The exact restore this app takes the most care over, end to end.

    ``json.dumps``'s default ``ensure_ascii`` escapes the lone surrogates
    ``os.fsdecode`` produces for a non-UTF-8 POSIX name into ``\\udcXX``, which
    is what lets the record be written AND read as pure ASCII. Drop it and the
    write raises ``UnicodeEncodeError`` inside the swallow-and-log arm: no record
    is left at all, the row degrades to an import-restore with the "trashed
    before origins were recorded" note, and the folder never goes back to the
    name it had.

    Both halves are asserted: the FILE is ASCII with the undecodable byte
    escaped, and the folder comes back at the byte-exact path it left.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    music = tmp_path / "music"
    husk_bytes = os.fsencode(str(music)) + b"/Caf\xe9 Album"
    husk = Path(os.fsdecode(husk_bytes))
    husk.mkdir()
    (husk / "booklet.jpg").write_bytes(b"\x00")

    dest = trash_folder(husk, trash_dir=tmp_path / "trash")

    raw = (dest / RECORD_NAME).read_bytes()
    raw.decode("ascii")  # the whole point of encoding="ascii": this cannot be assumed
    assert rb"\udce9" in raw, "the undecodable byte must survive as an escape"
    assert _record(dest).origin == os.fsdecode(husk_bytes)

    result = restore_album(lib, str(dest), trash_dir=tmp_path / "trash")

    assert result.restored is True
    assert (husk / "booklet.jpg").is_file()
    assert not dest.exists()
    assert os.fsencode(str(husk)) == husk_bytes  # byte-exact, not a lookalike
