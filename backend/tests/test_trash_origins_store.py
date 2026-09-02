"""The origin STORE's own promises: whose record it hands back, and never raising.

``test_trash_origin_record.py`` covers the feature — a trashed folder keeps
enough to be put back exactly. This file covers the three properties of the store
underneath it that the feature quietly assumes, each of which survived a mutation
of its own production line before these tests existed:

* a record is only ever read back for the entry it was written FOR, which the
  truncated key makes a real question rather than a tautology;
* :func:`delete_trash_origin` really never raises — every one of its callers runs
  it AFTER irreversible work, so an escaping exception 500s an operation that
  fully succeeded;
* a crafted Trash entry name cannot forge a log line on the write path, the one
  of the module's three ``%r`` sites that nothing pinned.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import pytest

from app.beets.trash_manage import list_trashed_albums
from app.beets.trash_origins import (
    delete_trash_origin,
    origin_file,
    origin_recorded,
    read_trash_origin,
    write_trash_origin,
)

# ----- a record belongs to ONE entry -----


def _colliding_pair() -> tuple[str, str]:
    """Two different entry names that :func:`origin_file` gives the same file.

    Written with LITERALS rather than derived from ``_MAX_KEY_BYTES``, so this is
    a construction against the key format and not a restatement of it: a test
    that recomputed the budget would follow the constant anywhere it moved and
    prove only self-consistency.

    ``long`` is 219 bytes, so ``<long>.json`` is 224 — one byte past the budget —
    and it is keyed as ``head[:201] + "." + sha256(long)[:16] + ".json"``.
    ``short`` is that stem spelled out: 218 bytes, so its own ``.json`` is 223 and
    is NOT truncated, and the two land on the same filename. Both fit
    ``NAME_MAX``, so both are names a real Trash entry can carry.
    """
    long_name = "N" * 219
    short_name = long_name[:201] + "." + hashlib.sha256(os.fsencode(long_name)).hexdigest()[:16]
    assert len(short_name) == 218
    return short_name, long_name


def test_the_two_names_that_share_a_record_file_really_do(tmp_path: Path) -> None:
    """The premise, stated on its own so the tests below cannot pass vacuously.

    If the key format ever stops mapping these two names onto one file, the
    collision tests would go green while testing nothing. This is the line that
    goes red instead.
    """
    short_name, long_name = _colliding_pair()
    assert short_name != long_name
    assert origin_file(tmp_path, short_name) == origin_file(tmp_path, long_name)


def test_a_record_is_never_read_back_for_a_different_entry(tmp_path: Path) -> None:
    """The truncated key is collidable by CONSTRUCTION, not by a birthday search.

    One sha256 call produces a second, shorter name with the same record file, so
    the two entries take turns owning it — and the loser's row does not lose an
    origin, it gains the WRONG one. That answer steers ``_move_no_merge``: the
    listing offers "Exact restore", and Restore renames the loser's folder into
    the winner's origin, on top of nothing that was ever there.

    Both directions, because "last writer wins" is not the property — "the reader
    knows whose record this is" is.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    short_name, long_name = _colliding_pair()

    write_trash_origin(origins, short_name, origin="/music/A/Short", moved="folder")
    write_trash_origin(origins, long_name, origin="/music/A/Long", moved="folder")

    long_record = read_trash_origin(origins, long_name)
    assert long_record is not None
    assert long_record.origin == "/music/A/Long"
    assert read_trash_origin(origins, short_name) is None, "it is the other entry's record now"

    # ...and the other way round, so this is not just "the last write wins".
    write_trash_origin(origins, short_name, origin="/music/A/Short", moved="folder")
    short_record = read_trash_origin(origins, short_name)
    assert short_record is not None
    assert short_record.origin == "/music/A/Short"
    assert read_trash_origin(origins, long_name) is None


def test_the_colliding_row_degrades_instead_of_pointing_at_a_strangers_folder(
    tmp_path: Path,
) -> None:
    """End to end at the listing, which is where the wrong answer would be acted on.

    Both folders really exist in Trash under their own names (both fit
    ``NAME_MAX``), both records were written by the ordinary writer, and the row
    that lost the file must read as the ordinary import-restore — the same row
    every pre-feature entry gets — rather than as an exact restore to a folder it
    has never been in.
    """
    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    music = tmp_path / "music"
    origins.mkdir()
    short_name, long_name = _colliding_pair()
    for name in (short_name, long_name):
        (trash / name).mkdir(parents=True)
    write_trash_origin(origins, short_name, origin=str(music / "Short"), moved="folder")
    write_trash_origin(origins, long_name, origin=str(music / "Long"), moved="folder")

    rows = {
        row.folder: row
        for row in list_trashed_albums(trash, origins_dir=origins, music_dir=str(music))
    }

    assert rows[long_name].restore_mode == "move_back"
    assert rows[long_name].origin == str(music / "Long")
    assert rows[short_name].restore_mode == "import"
    assert rows[short_name].origin is None, "a row must never be offered a stranger's origin"


def test_the_allocator_still_sees_a_colliding_key_as_occupied(tmp_path: Path) -> None:
    """``origin_recorded`` answers on EXISTENCE, and the asymmetry is the point.

    The reader refuses the loser's key; the allocator must NOT, or it would hand
    that name to a new folder and put the pair back in the same file. Occupied
    costs a burnt name (``<name> (1)``), which is the residual the store already
    accepts for a record whose entry was deleted outside the app.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    short_name, long_name = _colliding_pair()

    write_trash_origin(origins, long_name, origin="/music/A/Long", moved="folder")

    assert read_trash_origin(origins, short_name) is None
    assert origin_recorded(origins, short_name) is True
    assert origin_recorded(origins, long_name) is True


# ----- delete_trash_origin is contractually incapable of raising -----


def test_a_record_that_cannot_be_unlinked_is_logged_and_swallowed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Both halves of "NEVER raises", on the one call that runs after the point of no return.

    Every caller is past irreversible work when it gets here — the folder has
    been moved back into the library, or emptied, or stranded by a failed undo
    ("Never raises, so it cannot make this path worse"). An exception escaping
    turns an operation that fully SUCCEEDED into a bare 500, and the user is told
    a restore failed that did not.

    Two ways to escape, and this pins both at once:

    * ``unlink(missing_ok=True)`` swallows ``FileNotFoundError`` and nothing
      else, so a read-only, full or damaged ``/data`` raises through it. Staged
      with a DIRECTORY at the record's own name (``EISDIR``) rather than
      ``chmod``: CI images run as root, where a read-only directory denies
      nothing and the test would report green having executed no failure.
    * the handler itself. The entry name is a real filesystem name, so it can be
      non-UTF-8, and the obvious escape spelling
      (``.encode("utf-8", "backslashreplace").decode("ascii")``) raises
      ``UnicodeDecodeError`` on ordinary accented text — inside the one handler
      that may not raise. Hence ``display_path``, and hence a name carrying both
      an accent and an undecodable byte.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    name = os.fsdecode(b"Caf\xc3\xa9 \xff Dummy")
    assert not name.isascii(), "the accent that breaks the naive escape spelling"
    assert "\udcff" in name, "and the undecodable byte that display_path is for"
    origin_file(origins, name).mkdir()

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, name)  # must return, not raise

    (record,) = caplog.records
    assert "could not remove the Trash origin record" in record.getMessage()
    # Readable, not mangled: the accent survives and only the undecodable byte
    # becomes the placeholder, which is what tells an operator which entry it is.
    assert "Café" in record.getMessage()
    assert "�" in record.getMessage()


# ----- the write path's own %r, the third of three -----


def test_a_failed_write_cannot_forge_a_log_line_through_the_entry_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``%r`` on the entry name, pinned exactly as its two siblings are.

    The other two are ``_warn_unusable`` (the record path) and ``trash_manage``'s
    failed-undo line; this one had no test, so ``%r`` -> ``%s`` survived here
    while being killed at both of them. A Trash entry's name comes from the
    album's own tags and ``_trash_container_name`` neutralises path separators
    and nothing else, so a newline or an ANSI escape in an ``albumartist``
    reaches this line — the line an operator reads when a delete has just lost
    its origin. Interpolated raw, a crafted album name writes whatever it likes
    into the server log at exactly that moment.

    The write is failed structurally (a regular FILE where the store's directory
    belongs, so the parent cannot be created), which is root-safe and is what a
    read-only ``/data`` looks like from here.
    """
    forged = "Dummy\x1b[31m\nCRITICAL:app:all clear"
    origins = tmp_path / "trash-origins"
    origins.write_bytes(b"not a directory")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        write_trash_origin(origins, forged, origin="/music/A", moved="folder")

    assert any("could not record the Trash origin" in r.getMessage() for r in caplog.records)
    assert "\x1b" not in caplog.text, "an ANSI escape reached the log"
    assert "\nCRITICAL" not in caplog.text, "a forged log line reached the log"
    # Escaped, not dropped: the operator still gets the entry they have to look at.
    assert "\\x1b" in caplog.text
    assert "\\n" in caplog.text
