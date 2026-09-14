"""The origin STORE's own promises: whose record it hands back, and what it swallows.

``test_trash_origin_record.py`` covers the feature — a trashed folder keeps
enough to be put back exactly. This file covers the properties of the store
underneath it that the feature quietly assumes, each of which survived a mutation
of its own production line before these tests existed:

* a record is only ever read back for the entry it was written FOR, and is only
  ever DELETED for the entry it was written for, which the truncated key makes a
  real question rather than a tautology;
* :func:`delete_trash_origin` swallows rather than raising, and the claim is the
  LIST rather than the word "never": every one of its callers runs it AFTER
  irreversible work, so an escaping exception 500s an operation that fully
  succeeded, and the file shapes it has been put in front of are the ones this
  file plants — unparseable, not an object, nameless, another entry's, nested
  past the JSON parser's limit, a directory at the key;
* a store the app cannot reach reads as "nothing recorded" and says so in the
  log, which is the one place that residual is visible;
* a crafted Trash entry name cannot forge a log line. Counted rather than
  recalled, by walking the module's AST for ``logger.*`` calls (re-run
  2026-09-14): NINE calls, eight of which interpolate something, ten ``%r``
  placeholders between them. Six of the eight are pinned here against a crafted
  entry name -- the write's warning, the delete's failure, BOTH kept-record
  refusals (the payload one and the store-could-not-answer one, two
  placeholders each), the allocator's "could not tell", and the store sweep. A
  seventh, ``_warn_unusable``, is pinned in ``test_trash_origin_record.py``
  (``test_an_unusable_record_cannot_forge_a_log_line_through_its_own_filename``).
  The count is a measurement, so re-run the walk rather than trusting this
  sentence after adding a line.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import socket
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.trash_manage import empty_all, empty_one, list_trashed_albums, restore_album
from app.beets.trash_origins import (
    _MAX_RECORD_BYTES,
    TrashOriginsStoreUnusableError,
    clear_trash_origins,
    delete_trash_origin,
    origin_file,
    origin_recorded,
    read_trash_origin,
    require_usable_store,
    write_trash_origin,
)
from tests.conftest import build_library, protected_for, write_leased

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"


@pytest.fixture(autouse=True)
def _serial() -> Iterator[None]:
    # Same posture as test_trash_origin_record: single-threaded, and the
    # starter's copy:yes as the manual default. Only the restore test below
    # imports anything, but an import session reads these globals and the
    # cheapest way to say so is once, for the module.
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


def _with_bystander(tmp_path: Path) -> Library:
    """A library holding one unrelated album that is really on disk.

    Copied from ``test_trash_origin_record._bystander`` rather than shared: a
    restore writes INTO the music library and so runs behind
    ``require_library_present``, and a library with no album anywhere on disk IS
    the dropped-share fixture — a restore test built on one would assert through
    a guard that should have refused it.
    """
    lib = build_library(str(tmp_path / "library.db"), str(tmp_path / "music"))
    dst = tmp_path / "music" / "Bystander" / "Album" / "01 t.flac"
    _tagged_flac(dst, artist="Bystander", album="Album", title="T", track=1)
    item = Item(album="Album", albumartist="Bystander", artist="Bystander", title="T", track=1)
    item.path = os.fsencode(str(dst))
    lib.add_album([item]).store()
    return lib


# ----- a record belongs to ONE entry -----


def _colliding_pair() -> tuple[str, str]:
    """Two different entry names that :func:`origin_file` gives the same file.

    Written with LITERALS rather than derived from ``_MAX_KEY_BYTES``, so this is
    a construction against the key format and not a restatement of it: a test
    that recomputed the budget would follow the constant anywhere it moved and
    prove only self-consistency.

    Both names are ASCII, so characters and bytes coincide here; the store's cut
    is in BYTES (see :func:`origin_file`, whose recipe is spelled that way).

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
    origin, it gains the WRONG one. That answer steers ``move_no_merge``: the
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


# ----- ...and a record is only ever DELETED for the entry it was written for -----


def test_dropping_the_losing_rows_record_keeps_the_winners(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The read refuses the loser's key; the DELETE must refuse it too.

    ``read_trash_origin`` answering ``None`` for the losing name is exactly what
    sends both delete sites down their "no record / unreadable record" path, so
    the refusal at the read is what creates this hazard rather than something
    separate from it. Unlinking by key alone destroys the WINNER's record while
    its folder is still sitting in Trash — measured before this guard existed:
    ``delete(short)`` left ``read(long)`` answering ``None``. That is a row that
    exists losing its exact restore permanently, which for an audio-free husk is
    no way back at all.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    short_name, long_name = _colliding_pair()
    write_trash_origin(origins, long_name, origin="/music/A/Long", moved="folder")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, short_name)

    survivor = read_trash_origin(origins, long_name)
    assert survivor is not None, "the other entry's record was unlinked by key alone"
    assert survivor.origin == "/music/A/Long"
    assert origin_file(origins, long_name).exists()
    assert any("kept the Trash origin record" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("label", "payload", "why"),
    [
        (
            "no name at all",
            '{"schema": 1, "origin": "/music/A", "moved": "folder"}',
            "it does not name a Trash entry",
        ),
        ("not an object", '["schema", 1]', "it is not an object"),
        ("not JSON", "{ not json at all", "it is not the ASCII JSON this writes"),
        (
            "a name that is not a string",
            '{"schema": 1, "name": 7, "origin": "/music/A"}',
            "it does not name a Trash entry",
        ),
    ],
)
def test_a_record_that_does_not_name_another_entry_is_dropped_and_said_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, label: str, payload: str, why: str
) -> None:
    """Only a positively identified STRANGER survives; every doubt still unlinks.

    ``restore_album``'s import arm relies on this: an unreadable record reaches
    ``delete_trash_origin`` precisely because ``read_trash_origin`` collapsed it
    to ``None``, and leaving it behind burns its name forever (the allocator
    reads it as occupied) for a file that steers nothing. So "keep what I cannot
    parse" is the wrong safe side here, and the four shapes below are the ones a
    pre-feature record, a truncated write and a hand edit actually produce.

    And each one SAYS so. All four used to return ``False`` in silence, so the
    easiest plant to make — a plain regular ASCII file at the key that is not
    JSON — was read, unlinked and never mentioned (measured 2026-09-14, security
    seat L-2). ``empty_all`` reaches this function with no listing having read
    the key, so the read side's own warning is not a substitute: this line is
    the only trace the file was ever there.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    origin_file(origins, "Dummy").write_text(payload, encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, "Dummy")

    assert not origin_file(origins, "Dummy").exists(), f"{label} must not be left behind"
    (record,) = caplog.records
    assert why in record.getMessage(), label
    assert "None of it was used" in record.getMessage(), "the delete side's clause"
    assert "falls back to a re-import restore" not in record.getMessage(), (
        "there is no row left to fall back"
    )


def test_emptying_the_losing_row_keeps_the_other_entrys_record(tmp_path: Path) -> None:
    """Through ``empty_one``, one of the two callers that reach this on a ``None``.

    Both folders are really in Trash under their own names (both fit
    ``NAME_MAX``). Emptying the short one is an ordinary user action on an
    ordinary row — its own record was never written, which the store lists as
    the pre-feature/failed-write case — and it must not take the long row's
    record with it, because that row is still there and still restorable.
    """
    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    short_name, long_name = _colliding_pair()
    for name in (short_name, long_name):
        (trash / name).mkdir(parents=True)
    write_trash_origin(origins, long_name, origin="/music/A/Long", moved="folder")

    assert (
        empty_one(
            str(trash / short_name),
            origins_dir=origins,
            protected=protected_for(trash_dir=trash, origins_dir=origins),
        ).removed
        == 1
    )

    assert not (trash / short_name).exists()
    survivor = read_trash_origin(origins, long_name)
    assert survivor is not None, "the row still in Trash lost its exact restore"
    assert survivor.origin == "/music/A/Long"


def test_an_import_restore_of_the_losing_row_keeps_the_other_entrys_record(
    tmp_path: Path,
) -> None:
    """Through ``restore_album``'s import arm, the other caller — end to end.

    The losing row reads as an import-restore (its key holds somebody else's
    record), the import lands, and the arm then drops "the record this restore
    outlived" unconditionally. Unconditional is right for the file this entry
    owns and wrong for this one file, and the difference is made inside
    ``delete_trash_origin`` rather than at the call site.
    """
    lib = _with_bystander(tmp_path)
    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    short_name, long_name = _colliding_pair()
    (trash / long_name).mkdir(parents=True)
    _tagged_flac(
        trash / short_name / "01 Mysterons.flac",
        artist="Portishead",
        album="Dummy",
        title="Mysterons",
        track=1,
    )
    write_trash_origin(origins, long_name, origin=str(tmp_path / "music" / "Long"), moved="folder")
    assert read_trash_origin(origins, short_name) is None, "the losing row has no record of its own"

    result = restore_album(
        lib,
        str(trash / short_name),
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(lib),
    )

    assert result.restored is True
    assert (tmp_path / "music" / "Portishead" / "Dummy").is_dir()
    survivor = read_trash_origin(origins, long_name)
    assert survivor is not None, "a restore of one row destroyed another row's record"
    assert survivor.origin == str(tmp_path / "music" / "Long")


@pytest.mark.skipif(not hasattr(fcntl, "F_SETLEASE"), reason="leases are a Linux thing")
def test_a_record_the_store_cannot_answer_for_is_kept_instead_of_unlinked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Proof that the bytes are not ours may unlink; "the store could not say" may not.

    ``_record_text``'s ``None`` collapsed four refusals and two of them prove
    only that the store could not answer. On this shared key that cost the OTHER
    entry its exact restore: measured 2026-09-13 (security seat M-1) with a
    mode-000 record, emptying the SHORT row unlinked the file the LONG row —
    still sitting in Trash — is restored from, and the log line said the entry it
    was keyed on had already been removed.

    The long row's record is the oracle, twice: it must survive the delete, and
    it must still READ once the fault is cleared, which is what says the file was
    kept intact rather than merely present.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    short_name, long_name = _colliding_pair()
    write_trash_origin(origins, long_name, origin="/music/A/Long", moved="folder")
    shared = origin_file(origins, long_name)
    assert shared == origin_file(origins, short_name), "the two names share one record file"

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        with write_leased(shared):
            delete_trash_origin(origins, short_name)
            assert shared.exists(), "a record the store could not read was unlinked"

    survivor = read_trash_origin(origins, long_name)
    assert survivor is not None, "the other entry's record did not survive intact"
    assert survivor.origin == "/music/A/Long"
    cause, kept = caplog.records
    assert "it could not be opened" in cause.getMessage()
    assert "None of it was used" in cause.getMessage(), "the read side's clause is false here"
    assert "left whatever is at" in kept.getMessage()
    assert "could not say what is there" in kept.getMessage()
    assert "names a different Trash entry" not in kept.getMessage(), (
        "nothing here read a payload, so nothing here can say whose it is"
    )


@pytest.mark.skipif(not hasattr(fcntl, "F_SETLEASE"), reason="leases are a Linux thing")
def test_keeping_a_record_the_store_cannot_read_cannot_forge_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The second kept-record line's two ``%r`` sites, on the same footing as the first.

    Both values it interpolates carry whatever the album's tags carried, and the
    entry name reaches the log through the record's own FILENAME.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    key = origin_file(origins, _FORGED_ENTRY_NAME)
    key.write_text('{"schema": 1, "name": "whoever", "origin": "/music/A"}', encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        with write_leased(key):
            delete_trash_origin(origins, _FORGED_ENTRY_NAME)

    assert key.exists(), "a record the store could not read was unlinked"
    assert any("left whatever is at" in r.getMessage() for r in caplog.records)
    _assert_nothing_forged(caplog)


#: The longest path ``socket.bind`` takes for AF_UNIX here, in bytes: ``sun_path``
#: is 108 (unix(7)) and a 108-byte path raised "AF_UNIX path too long" while a
#: 107-byte one bound (measured 2026-09-14).
_AF_UNIX_PATH_BYTES = 107


def _bind_or_skip(sock: socket.socket, key: Path) -> None:
    """Bind ``sock`` at ``key``, skipping only when ``key`` is too long to be a socket.

    Measured on the key itself: pytest's ``tmp_path`` adds its own directories
    and part of the test's name below ``TMPDIR``, so a bound on ``TMPDIR``
    failed at a 49-byte one (code seat W4).
    """
    length = len(os.fsencode(str(key)))
    if length > _AF_UNIX_PATH_BYTES:
        pytest.skip(f"the key is {length} bytes; an AF_UNIX path takes {_AF_UNIX_PATH_BYTES}")
    sock.bind(str(key))


def test_a_socket_at_the_key_is_dropped_like_any_other_plant(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A socket stats as a socket, so it is refused on its name and still goes.

    That puts it in the same class as a FIFO, a directory or a device: the bytes
    at the key are not this store's, and a plant nothing unlinks would hold its
    Trash name forever.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    key = origin_file(origins, "Dummy")
    with socket.socket(socket.AF_UNIX) as sock:
        _bind_or_skip(sock, key)
        with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
            delete_trash_origin(origins, "Dummy")

    assert not os.path.lexists(key), "the socket outlived the entry it was keyed on"
    (record,) = caplog.records
    assert "it is not a regular file" in record.getMessage()


def _before_the_first_open_of(
    monkeypatch: pytest.MonkeyPatch, key: Path, change: Callable[[], None]
) -> None:
    """Run ``change`` once, between the gate's ``stat`` of ``key`` and its ``open``."""
    real_open = os.open
    pending = [change]

    def spy_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if pending and os.fsdecode(path) == str(key):
            pending.pop()()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)


def test_a_socket_swapped_onto_the_key_after_its_stat_is_still_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENXIO from the open is PROOF, not a fault.

    A socket already at the key is refused by the ``stat``, so the open meets
    one only when the name changes in between. It still answers about the NAME
    (a socket is not a file at all), which is why it is not the "store could not
    answer" class that keeps the file.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    key = origin_file(origins, "Dummy")
    key.write_text("{}", encoding="ascii")
    with socket.socket(socket.AF_UNIX) as sock:

        def plant() -> None:
            key.unlink()
            _bind_or_skip(sock, key)

        _before_the_first_open_of(monkeypatch, key, plant)
        with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
            delete_trash_origin(origins, "Dummy")

    assert not os.path.lexists(key), "the socket outlived the entry it was keyed on"
    (record,) = caplog.records
    assert "it does not lead to a file" in record.getMessage()


def _rename_a_rewritten_record_on(monkeypatch: pytest.MonkeyPatch, key: Path) -> None:
    payload = key.read_text(encoding="ascii")

    def rewrite() -> None:
        staged = key.with_name("staged.tmp")
        staged.write_text(payload, encoding="ascii")
        os.replace(staged, key)

    _before_the_first_open_of(monkeypatch, key, rewrite)


def _rename_a_fifo_on(monkeypatch: pytest.MonkeyPatch, key: Path) -> None:
    def swap() -> None:
        # Made under its own name while the record is still linked: an unlink
        # then ``mkfifo`` at the key can reuse the record's inode number on ext4
        # (code seat), which the identity check cannot see.
        staged = key.with_name("staged.fifo")
        os.mkfifo(staged)
        os.replace(staged, key)

    _before_the_first_open_of(monkeypatch, key, swap)


def _stat_the_key_on_another_device(monkeypatch: pytest.MonkeyPatch, key: Path) -> None:
    real_stat = os.stat

    def stat_elsewhere(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        found = real_stat(path, *args, **kwargs)
        if os.fsdecode(path) != str(key):
            return found
        fields = list(tuple(found))
        fields[stat.ST_DEV] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "stat", stat_elsewhere)


@pytest.mark.parametrize(
    "replace",
    [
        pytest.param(_rename_a_rewritten_record_on, id="a-rewritten-record"),
        pytest.param(_rename_a_fifo_on, id="a-fifo"),
        pytest.param(_stat_the_key_on_another_device, id="the-same-inode-number-on-another-device"),
    ],
)
def test_a_record_replaced_between_its_stat_and_its_open_is_kept(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    replace: Callable[[pytest.MonkeyPatch, Path], None],
) -> None:
    """A different ``(st_dev, st_ino)`` behind the name is a swap, and a swap is not proof.

    ``write_trash_origin`` rewrites a record with ``os.replace``, which is a new
    inode behind the same name, so the delete keeps what it met. Each case pins
    one part of the identity check, measured by mutating it (fix round 7):

    * the rewritten record names this very entry, so without the check it is
      read and unlinked;
    * the FIFO pins the ORDER: with the type asked before the identity, it is
      classed as proof and unlinked (MX4);
    * the other device differs in ``st_dev`` only, so comparing ``st_ino``
      alone reads and unlinks it (MX3).

    On a thread with a join deadline, because the FIFO really is opened.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    write_trash_origin(origins, "Dummy", origin="/music/A", moved="folder")
    key = origin_file(origins, "Dummy")
    replace(monkeypatch, key)

    worker = threading.Thread(target=delete_trash_origin, args=(origins, "Dummy"), daemon=True)
    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        worker.start()
        worker.join(10)

    assert not worker.is_alive(), "the delete is still blocked opening the key"
    assert os.path.lexists(key), "a record replaced in the window was unlinked"
    cause, kept = caplog.records
    assert "it was replaced while it was being opened" in cause.getMessage()
    assert "could not say what is" in kept.getMessage()


def test_a_descriptor_that_is_not_a_regular_file_is_refused_when_the_stat_agreed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``fstat`` still decides the type, for the one case the ``stat`` cannot.

    A matching ``(st_dev, st_ino)`` with a different type is a reused inode
    number. That cannot be forced on demand, so ``os.stat`` is made to report the
    FIFO at the key as a regular file with its own identity, which is what a
    reuse looks like from inside the gate.

    Run on a thread with a join deadline: that FIFO really is opened here, so
    without ``O_NONBLOCK`` the open blocks, and a plain call would hang this suite
    instead of failing it.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    key = origin_file(origins, "Dummy")
    os.mkfifo(key)
    real_stat = os.stat

    def stat_as_regular(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        found = real_stat(path, *args, **kwargs)
        if os.fsdecode(path) != str(key):
            return found
        return os.stat_result((stat.S_IFREG | 0o644, *tuple(found)[1:]))

    monkeypatch.setattr(os, "stat", stat_as_regular)
    worker = threading.Thread(target=delete_trash_origin, args=(origins, "Dummy"), daemon=True)
    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        worker.start()
        worker.join(10)

    assert not worker.is_alive(), "the delete is still blocked opening the FIFO"
    assert not os.path.lexists(key), "the FIFO outlived the entry it was keyed on"
    (record,) = caplog.records
    assert "it is not a regular file" in record.getMessage()


@pytest.mark.parametrize(
    ("plant", "why"),
    [
        ("a dangling link", "it is a link to something that is not there"),
        ("a non-ASCII byte", "it is not the ASCII JSON this writes"),
        pytest.param(
            "a link to /proc/kallsyms",
            "it is far too large to be a record",
            marks=pytest.mark.skipif(
                not os.access("/proc/kallsyms", os.R_OK),
                reason="no readable /proc/kallsyms on this box",
            ),
        ),
    ],
)
def test_a_proof_refusal_unlinks_the_plant_on_the_delete_side(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, plant: str, why: str
) -> None:
    """The refusal CLASS decides unlink or keep, so each proof site needs its own delete.

    Flipping the class to "store-unreachable" at the dangling-link site, the
    grew-past-the-cap site or the decode site left 168 tests green (code seat W1):
    each such plant would have been kept for good. The key being gone is what the
    proof class does. ``kallsyms`` stats at 0 bytes and reads past the cap, so it
    is the read bound's site and not the ``st_size`` one.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    key = origin_file(origins, "Dummy")
    if plant == "a dangling link":
        key.symlink_to(origins / "nowhere.json")
    elif plant == "a non-ASCII byte":
        key.write_bytes(b'{"name": "Dummy", "x": "\xe9"}')
    else:
        key.symlink_to("/proc/kallsyms")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, "Dummy")

    assert not os.path.lexists(key), f"{plant} outlived the entry it was keyed on"
    (record,) = caplog.records
    assert why in record.getMessage()


@pytest.mark.parametrize(
    ("fault", "why"),
    [
        ("a read that fails with EIO", "it could not be read"),
        ("a store path that is a regular file", "it could not be looked up"),
    ],
)
def test_a_store_fault_leaves_the_key_alone_and_says_so_twice(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    why: str,
) -> None:
    """The "store-unreachable" sites a lease does not reach, each with both log lines.

    A read that raised had no test at all: classing it as proof, or deleting its
    log line, left 168 tests green (code seat W1). EIO is injected at ``os.read``
    because failing storage cannot be staged, and a mode-000 file is readable by
    root. A store path that is a regular file fails the ``stat`` with ENOTDIR, and
    nothing is behind it, which is why the kept line may not claim a record is
    there (code seat S4).
    """
    origins = tmp_path / "trash-origins"
    if fault == "a read that fails with EIO":
        origins.mkdir()
        write_trash_origin(origins, "Dummy", origin="/music/A", moved="folder")

        def failing_read(fd: int, length: int) -> bytes:
            raise OSError(errno.EIO, os.strerror(errno.EIO))

        monkeypatch.setattr(os, "read", failing_read)
    else:
        origins.write_text("not a directory", encoding="ascii")
    key = origin_file(origins, "Dummy")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, "Dummy")
    monkeypatch.undo()

    if fault == "a read that fails with EIO":
        assert key.exists(), "a record the store could not read was unlinked"
    else:
        assert origins.read_text(encoding="ascii") == "not a directory"
    cause, kept = caplog.records
    assert why in cause.getMessage()
    assert "None of it was used" in cause.getMessage()
    assert "left whatever is at" in kept.getMessage()
    assert "If a record is there" in kept.getMessage()


# ----- the DELETE side reads the key too, and through the same gate -----


def _one_entry_to_empty(tmp_path: Path) -> tuple[Path, Path]:
    """A Trash holding one recorded entry, and the store beside it."""
    trash, origins = tmp_path / "trash", tmp_path / "trash-origins"
    origins.mkdir()
    (trash / "Album").mkdir(parents=True)
    (trash / "Album" / "a.flac").write_bytes(b"\x00")
    write_trash_origin(origins, "Album", origin=str(tmp_path / "music" / "Album"), moved="folder")
    return trash, origins


def test_a_fifo_at_the_key_does_not_hang_the_delete_that_reads_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``empty_one`` reads the record key AFTER the entry is already gone.

    ``delete_trash_origin`` asks whose record the file is before unlinking it,
    and that read had a bare ``read_text`` for a day while the LISTING's read
    two hundred lines above had a ``stat`` gate. Every route that takes an entry
    out of Trash reaches this one -- restore, both Empties, and trashing an
    album -- all of them inside ``beets_swap_lock``. Measured 2026-09-13
    (security seat H-1) and re-measured 2026-09-14 here: a FIFO at
    ``<origins>/Album.json`` left ``empty_one`` blocked past a 6 s deadline with
    the entry ALREADY destroyed, so the lock was held for the life of the
    process and every later mutating Trash route and both reorganize previews
    answered 409.

    The plant is unlinked rather than kept: ``None`` from the gate means "not a
    record this store can read", which names nobody, so the store repairs
    itself the first time an operator touches the entry.

    Run on a thread with a join deadline, because a plain call would hang this
    suite instead of failing it.
    """
    trash, origins = _one_entry_to_empty(tmp_path)
    key = origin_file(origins, "Album")
    key.unlink()
    os.mkfifo(key)
    done: list[object] = []

    def run() -> None:
        done.append(
            empty_one(
                str(trash / "Album"),
                origins_dir=origins,
                protected=protected_for(trash_dir=trash, origins_dir=origins),
            ).removed
        )

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(10)

    assert not worker.is_alive(), "the delete is still blocked on the FIFO at the record key"
    assert done == [1]
    assert not (trash / "Album").exists()
    assert not os.path.lexists(key), "the plant outlived the entry it was keyed on"
    (record,) = caplog.records
    assert "it is not a regular file" in record.getMessage()
    assert "Album.json" in record.getMessage()


def test_a_file_too_large_to_be_a_record_is_refused_on_the_delete_path_too(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The cap reaches this reader as well, and the sentence is the oracle.

    Separately from the FIFO above, because a partial revert -- one reader
    getting ``S_ISREG`` back and not the cap -- passes that test and fails this
    one. The cost this bounds is memory rather than a wedge: measured
    2026-09-14, a 400 MB sparse file at the key put ``empty_one`` at +801 MB RSS
    in 150 ms, once per Empty click.

    A literal size, not ``_MAX_RECORD_BYTES + 1``: a fixture computed from the
    constant follows it and cannot see it move.
    """
    trash, origins = _one_entry_to_empty(tmp_path)
    key = origin_file(origins, "Album")
    planted = 8 * 1024 * 1024
    with key.open("wb") as fh:
        fh.truncate(planted)  # sparse -- no bytes are written and none are read
    assert _MAX_RECORD_BYTES < planted, "the fixture has to be over the cap"

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        removed = empty_one(
            str(trash / "Album"),
            origins_dir=origins,
            protected=protected_for(trash_dir=trash, origins_dir=origins),
        ).removed

    assert removed == 1
    assert not key.exists(), "the plant outlived the entry it was keyed on"
    (record,) = caplog.records
    assert "far too large to be a record" in record.getMessage()


def test_empty_all_sweeps_past_a_planted_key_instead_of_stopping_on_it(
    tmp_path: Path,
) -> None:
    """The aggravation the per-row test cannot show: the REST of the sweep.

    ``empty_all`` drops each record inside the loop, so a key it blocks on
    abandons every entry after it and the operator gets no response at all --
    measured 2026-09-14 with a FIFO at the first entry's key: the folder was
    removed, the read hung past a 6 s deadline, and ``Beta`` was still in Trash
    with nothing reported. The count is the oracle, because a sweep that stops
    half way still removed something.

    Run on a thread with a join deadline, for the reason above.
    """
    trash, origins = _one_entry_to_empty(tmp_path)
    (trash / "Beta").mkdir()
    (trash / "Beta" / "b.flac").write_bytes(b"\x00")
    key = origin_file(origins, "Album")  # "Album" sorts first, so it blocks the rest
    key.unlink()
    os.mkfifo(key)
    done: list[object] = []

    def run() -> None:
        done.append(
            empty_all(
                trash,
                origins_dir=origins,
                protected=protected_for(trash_dir=trash, origins_dir=origins),
            ).removed
        )

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)

    assert not worker.is_alive(), "the sweep is still blocked on the FIFO at the first key"
    assert done == [2], "the sweep stopped at the planted key"
    assert list(trash.iterdir()) == []


# ----- delete_trash_origin swallows what its callers cannot handle -----


def test_a_record_that_cannot_be_unlinked_is_logged_and_swallowed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The swallow, on the one call that runs after the point of no return.

    Every caller is past irreversible work when it gets here — the folder has
    been moved back into the library, or emptied, or stranded by a failed undo
    ("Never raises, so it cannot make this path worse"). An exception escaping
    turns an operation that fully SUCCEEDED into a bare 500, and the user is told
    a restore failed that did not.

    Two ways to escape, and this pins both at once:

    * ``unlink(missing_ok=True)`` swallows ``FileNotFoundError`` and nothing
      else, so a read-only, full or damaged ``/data`` raises through it. Staged
      with a NON-EMPTY DIRECTORY at the record's own name (``EISDIR``, then
      ``ENOTEMPTY``) rather than ``chmod``: a maintainer running this suite
      inside the shipped image is root (``Dockerfile`` declares no ``USER``),
      and there a read-only directory denies nothing, so a chmod-staged test
      would report green having executed no failure. Those two errnos deny root
      too. CI is not the case this guards against — its pytest job runs as
      ``runner``. Non-empty is load-bearing since 2026-09-14: an EMPTY
      directory at the key is now removed by the ``rmdir`` arm (the test below
      this one), so an empty plant would leave nothing to swallow.
    * the handler itself. The entry name is a real filesystem name, so it can be
      non-UTF-8, and the obvious escape spelling
      (``.encode("utf-8", "backslashreplace").decode("ascii")``) raises
      ``UnicodeDecodeError`` on ordinary accented text — inside the one handler
      that may not raise. Hence ``display_path``, and hence a name carrying both
      an accent and an undecodable byte.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    name = os.fsdecode(b"Caf\xc3\xa9 \xff \x1b[31m\nCRITICAL:app:all clear")
    assert not name.isascii(), "the accent that breaks the naive escape spelling"
    assert "\udcff" in name, "and the undecodable byte that display_path is for"
    planted = origin_file(origins, name)
    planted.mkdir()
    (planted / "something").write_text("not this function's to delete", encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, name)  # must return, not raise

    # TWO lines, and they say different things: the shared record gate refuses
    # the directory before any read ("not a regular file"), then the ``unlink``
    # fails and this function reports that. The gate's line deliberately claims
    # nothing about the unlink -- it used to say "so the file goes too", which
    # this very fixture falsifies.
    gate, record = caplog.records
    assert "it is not a regular file" in gate.getMessage()
    assert "None of it was used" in gate.getMessage(), "the read side's clause is false here"
    assert "falls back to a re-import restore" not in gate.getMessage()
    assert "could not remove the Trash origin record" in record.getMessage()
    # Readable, not mangled: the accent survives and only the undecodable byte
    # becomes the placeholder, which is what tells an operator which entry it is.
    assert "Café" in record.getMessage()
    assert "�" in record.getMessage()
    # ...and the ``%r`` is load-bearing on this line too. ``display_path``
    # neutralises the undecodable byte and NOTHING else, so an ANSI escape and a
    # newline in the same name — both ordinary characters in an ``albumartist``
    # — reach the log unless the interpolation quotes them. Spelled ``%s`` the
    # two assertions above still pass, which is how this site stayed unpinned.
    assert "\x1b" not in caplog.text, "an ANSI escape reached the log"
    assert "\nCRITICAL" not in caplog.text, "a forged log line reached the log"
    assert "\\x1b" in caplog.text  # escaped, not dropped
    assert "\\n" in caplog.text
    assert planted.is_dir(), "a directory with something in it is not this function's to delete"


def test_an_empty_directory_at_the_key_is_removed_rather_than_burning_the_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The control for the swallow above, and the reason its fixture is non-empty.

    ``S_ISREG`` refuses a directory at the key and ``unlink`` cannot remove it,
    so before the ``rmdir`` arm nothing in the app could ever clear one —
    measured 2026-09-14 (security seat L-3), including over HTTP (``DELETE
    /api/albums/{id}`` answered 500 in 0.006 s and the plant survived). The cost
    is not the 500: ``origin_recorded`` answers on EXISTENCE, so that Trash name
    stayed occupied and every later album trashed under it landed on ``<name>
    (1)``, ``(2)``… with no exact-restore record of its own.

    ``origin_recorded`` is the oracle rather than the missing directory, because
    it is what the allocator asks.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    key = origin_file(origins, "Album")
    key.mkdir()
    assert origin_recorded(origins, "Album") is True, "the plant holds the name to begin with"

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, "Album")

    assert origin_recorded(origins, "Album") is False, "the name is still held against an album"
    assert not key.exists()
    (gate,) = caplog.records
    assert "it is not a regular file" in gate.getMessage()
    assert "could not remove the Trash origin record" not in caplog.text, (
        "the rmdir succeeded, so there is nothing to report"
    )


# ----- ...including a file the JSON parser gives up on -----


def _deeply_nested_json() -> str:
    """A legal-ASCII, legal-JSON file that ``json.loads`` refuses to finish.

    24,000 nested arrays, and the depth now has to fit a window bounded at BOTH
    ends, so both ends are asserted rather than assumed:

    * **Deep enough that the parser gives up.** Not a threshold this suite owns
      — it is CPython's C scanner limit, not ``sys.getrecursionlimit()`` (1000
      here, and irrelevant): measured 2026-09-13, the shallowest nest that
      raises is **9,998** levels. If a build ever swallows this one, the
      ``pytest.raises`` below goes red instead of the tests going quietly green.
    * **Small enough that the SIZE cap does not answer first.** ``read_trash_origin``
      grew a 64 KiB cap on 2026-09-13 (security seat M-1), which sits ABOVE this
      arm: at the old 60,000 levels this file was 117 KiB and earned "far too
      large to be a record" instead of the nesting sentence. That did not make
      the nesting arm dead code — the parser gives up around 20 KB, a third of
      the cap — but it does mean the fixture has to say where it sits, and the
      assertion below is that sentence.

    The trigger is corruption, a hand edit, a restored backup, or something
    planted at the key if the store sits where the operator can be reached; what
    is new is not that the file is unusable — the store has always had unusable
    shapes — but that reading it raised AFTER the entry was already destroyed.
    """
    text = "[" * 24_000 + "]" * 24_000
    with pytest.raises(RecursionError):
        json.loads(text)
    assert len(text) < _MAX_RECORD_BYTES, "the size cap would answer before the parser could"
    return text


def test_a_record_the_json_parser_gives_up_on_reads_as_no_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``RecursionError`` is a ``RuntimeError``, so the parse arm walked past it.

    Measured at the previous tip: this file at the key raised straight out of
    ``read_trash_origin``, which is called once per row by
    ``list_trashed_albums`` — so one corrupt record 500'd the whole Trash page,
    including every other row on it. Asserted through the listing for that
    reason, and not only at the store.

    The bystander row is what makes the 500 visible as a blast radius rather
    than as one bad row: it has a perfectly good record and must still be listed
    with its exact restore.
    """
    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    music = tmp_path / "music"
    origins.mkdir()
    for name in ("Deep", "Fine"):
        (trash / name).mkdir(parents=True)
    write_trash_origin(origins, "Fine", origin=str(music / "Fine"), moved="folder")
    origin_file(origins, "Deep").write_text(_deeply_nested_json(), encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        assert read_trash_origin(origins, "Deep") is None
        rows = {
            row.folder: row
            for row in list_trashed_albums(trash, origins_dir=origins, music_dir=str(music))
        }

    assert rows["Deep"].restore_mode == "import"
    assert rows["Fine"].restore_mode == "move_back", "one bad record took the whole listing"
    assert any("present but unusable" in r.getMessage() for r in caplog.records)
    assert any("nests deeper" in r.getMessage() for r in caplog.records), (
        "the operator gets this record's own cause, not the parse arm's sentence"
    )


def test_a_record_the_json_parser_gives_up_on_does_not_escape_empty_one(
    tmp_path: Path,
) -> None:
    """The raise that landed AFTER the irreversible work, which is what makes it a bug.

    ``empty_one`` rmtree's the entry and drops its record on the next line, and
    ``delete_trash_origin`` reads the payload first to see whose record it is
    holding. Measured at the previous tip: the folder was gone, the record was
    still there, and the route answered 500 — the user is told the empty failed
    while the album is already unrecoverable, and every retry says the same.

    The record goes, which is the same answer every other shape this store
    cannot identify gets (``test_a_record_that_does_not_name_another_entry_is_
    still_dropped``): it names nobody, so it steers nothing, and leaving it
    would burn its name for good.
    """
    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    (trash / "Deep").mkdir(parents=True)
    (trash / "Deep" / "01 t.flac").write_bytes(b"\x00")
    origin_file(origins, "Deep").write_text(_deeply_nested_json(), encoding="ascii")

    assert (
        empty_one(
            str(trash / "Deep"),
            origins_dir=origins,
            protected=protected_for(trash_dir=trash, origins_dir=origins),
        ).removed
        == 1
    )

    assert not (trash / "Deep").exists()
    assert not origin_file(origins, "Deep").exists()


def test_a_record_the_json_parser_gives_up_on_does_not_abort_empty_all(
    tmp_path: Path,
) -> None:
    """The same raise in the loop, where it takes the entries it has not reached yet.

    ``empty_all``'s per-entry ``except OSError`` is what lets one unremovable
    folder be reported rather than abort the sweep, and a ``RecursionError``
    from the record drop is outside it. Measured at the previous tip on this
    fixture: the sweep destroyed the first entry, raised, and left the other two
    in Trash with the operation reported as a failure.

    ``iterdir`` order is not defined, so the bad record is put on EVERY entry —
    the assertion is then about the sweep finishing, not about which child
    happened to come first.
    """
    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    deep = _deeply_nested_json()
    for name in ("A Album", "B Album", "C Album"):
        (trash / name).mkdir(parents=True)
        (trash / name / "01 t.flac").write_bytes(b"\x00")
        origin_file(origins, name).write_text(deep, encoding="ascii")

    assert (
        empty_all(
            trash,
            origins_dir=origins,
            protected=protected_for(trash_dir=trash, origins_dir=origins),
        ).removed
        == 3
    )

    assert list(trash.iterdir()) == []


def test_clearing_a_store_that_was_never_created_is_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The SILENCE is the point, exactly as it is for a record never written.

    A deployment that has never recorded an origin has no store directory, and
    every Empty all it runs reaches the sweep. Spelled with one ``except
    OSError`` the missing directory would earn a WARNING every time — noise that
    buries the warnings that mean something, since a record present and
    unusable is how a failing ``/data`` announces itself.
    """
    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        clear_trash_origins(tmp_path / "never-created")

    assert caplog.records == []


# ----- the two log lines whose %r nothing was pinning -----

#: A Trash entry name that writes a fake log line if it is interpolated raw. It
#: needs nothing hostile to exist: ``_trash_container_name`` builds the name from
#: the album's own ``albumartist``/``album`` tags and neutralises separators, NUL
#: and U+FFFD but no other control character, so an ANSI escape or a newline in
#: a tag arrives here intact.
_FORGED_ENTRY_NAME = "Dummy\x1b[31m\nCRITICAL:app:all clear"


def _assert_nothing_forged(caplog: pytest.LogCaptureFixture) -> None:
    """The escape survived as text and not as control characters."""
    assert "\x1b" not in caplog.text, "an ANSI escape reached the log"
    assert "\nCRITICAL" not in caplog.text, "a forged log line reached the log"
    # Escaped rather than dropped: the operator still gets the entry to look at.
    assert "\\x1b" in caplog.text
    assert "\\n" in caplog.text


def _origins_dir_over_path_max(tmp_path: Path, entry_name: str) -> Path:
    """An origins dir nested until its record for ``entry_name`` is past PATH_MAX.

    Every component stays inside ``NAME_MAX`` and the origins dir itself stays
    inside ``PATH_MAX``, so every ``mkdir`` on the way succeeds: the only path
    the kernel refuses is the record file whose existence ``origin_recorded`` is
    about to test. Measured against ``os.pathconf`` rather than a literal,
    because what has to be true is that the KERNEL refuses it — and nothing is
    monkeypatched, so this is a real ``OSError`` with a real errno.
    """
    path_max = os.pathconf(str(tmp_path), "PC_PATH_MAX")
    key = origin_file(Path("/"), entry_name).name
    deep = tmp_path
    while len(str(deep / ("d" * 200) / "trash-origins")) < path_max:
        deep = deep / ("d" * 200)
    # ...then one last component sized to land the store just inside the limit,
    # since another 200-byte one would put the store itself past it.
    gap = path_max - len(str(deep / "trash-origins"))
    if gap >= 3:
        deep = deep / ("d" * (gap - 2))
    origins = deep / "trash-origins"
    origins.mkdir(parents=True)
    assert len(str(origins)) < path_max, "the store itself must be a legal path"
    assert len(str(origins / key)) > path_max, "...and its record file must not be"
    return origins


def test_a_name_the_allocator_cannot_check_cannot_forge_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``origin_recorded``'s warning, and what it is now careful NOT to blame.

    This line runs at the exact moment the KEY-level residual is created: the
    name reads as free although a record may be sitting on it, so the allocator
    can hand it to a second folder that will later read the first folder's
    origin. It is the only trace of that, and it is reached with a name the
    album's own tags can produce. The STORE-level version of the same shape is
    no longer a residual at all — it refuses the delete
    (``test_a_store_that_cannot_be_searched_refuses_instead_of_reading_as_free``
    below) — which is why this arm's errno matters: ENAMETOOLONG says nothing
    about whether the store works, so it keeps the old answer and this warning.

    The sentence used to end "Check the permissions on the Trash origins
    directory". This fixture is the counter-example, and it needs no injection
    and no root: an origins dir nested until the record path passes ``PATH_MAX``
    makes ``exists()`` raise ENAMETOOLONG, where a permissions hunt finds
    nothing wrong. So the cause is left to ``exc_info`` — asserted below by the
    errno the traceback carries, which is also what pins ``exc_info`` itself
    against being dropped.
    """
    origins = _origins_dir_over_path_max(tmp_path, _FORGED_ENTRY_NAME)
    # The fixture is really at the hazard rather than merely deep.
    record = origin_file(origins, _FORGED_ENTRY_NAME)
    with pytest.raises(OSError) as raw:
        record.exists()
    assert raw.value.errno == errno.ENAMETOOLONG

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        assert origin_recorded(origins, _FORGED_ENTRY_NAME) is False

    assert any(
        "could not tell whether a Trash origin record exists" in r.getMessage()
        for r in caplog.records
    )
    _assert_nothing_forged(caplog)
    assert f"[Errno {errno.ENAMETOOLONG}]" in caplog.text, (
        "the traceback is where the cause is now named, so it has to be there"
    )


def test_keeping_another_entrys_record_cannot_forge_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The kept-record warning: both its ``%r`` sites, and what it can honestly claim.

    Reached without the colliding pair, because the refusal is about the
    PAYLOAD: any record whose ``name`` is not the entry being deleted is kept,
    and both of the values this line interpolates — the record's path and the
    entry name — carry whatever the album's tags carried.

    It also says what the function knows. There is no Trash directory in this
    fixture at all, and ``delete_trash_origin`` is not given one: the record
    naming "Another Album" is kept on the strength of the payload alone, and
    whether "Another Album" is still in Trash is not checked and cannot be from
    here. The line used to assert it was ("still in Trash and still needs it");
    when that guess is wrong the file is exactly the leftover ``empty_all``
    sweeps.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    payload = {"schema": 1, "name": "Another Album", "origin": "/music/A", "moved": "folder"}
    origin_file(origins, _FORGED_ENTRY_NAME).write_text(json.dumps(payload), encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        delete_trash_origin(origins, _FORGED_ENTRY_NAME)

    assert origin_file(origins, _FORGED_ENTRY_NAME).exists(), "another entry's record was dropped"
    assert any("kept the Trash origin record" in r.getMessage() for r in caplog.records)
    _assert_nothing_forged(caplog)


# ----- clearing the whole store -----


def test_clearing_the_store_carries_on_past_a_record_it_cannot_remove(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``clear_trash_origins`` runs after Trash is already empty, so it may not raise.

    Same position as :func:`delete_trash_origin` and the same posture: every
    entry is gone by the time this runs, so an exception escaping would report a
    completed Empty all as a 500, and one unremovable file would take every
    other record's cleanup with it.

    Staged with a DIRECTORY at a record's name (``EISDIR`` on ``unlink``) rather
    than a ``chmod``, because CI runs the suite as ``runner`` on one machine and
    as root elsewhere, where a mode bit denies nothing and the test would report
    green having exercised no failure at all.

    The name is also the log line's own pin: a record's filename is a Trash
    entry's name, which comes from the album's tags, so an ANSI escape or a
    newline in an ``albumartist`` reaches this warning through the path. ``%r``
    is what stops it forging a line.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    forged = _FORGED_ENTRY_NAME
    write_trash_origin(origins, "Ordinary", origin="/music/A/Ordinary", moved="folder")
    origin_file(origins, forged).mkdir()

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        clear_trash_origins(origins)  # must return, not raise

    assert not origin_file(origins, "Ordinary").exists(), "one bad file stopped the sweep"
    assert origin_file(origins, forged).is_dir()
    assert any("could not remove the Trash origin record" in r.getMessage() for r in caplog.records)
    _assert_nothing_forged(caplog)


def test_clearing_the_store_removes_the_records_and_nothing_else(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The sweep is keyed on the ``.json`` the store writes, not on "everything here".

    MusicDrop creates the origins dir and owns it, but owning it is not the same
    as being the only writer: a note an operator keeps beside the records, or a
    directory a backup tool leaves there, is not something emptying Trash has
    any reason to delete. Only what :func:`origin_file` writes is swept.

    Planted together rather than as two tests, because the pair is the claim:
    the record still has to go in the same call that leaves the other two alone.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    write_trash_origin(origins, "Ordinary", origin="/music/A/Ordinary", moved="folder")
    note = origins / "notes.txt"
    note.write_text("operator's own", encoding="ascii")
    kept = origins / "backup"
    kept.mkdir()
    (kept / "Ordinary.json").write_text("{}", encoding="ascii")

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        clear_trash_origins(origins)

    assert not origin_file(origins, "Ordinary").exists(), "the record itself had to go"
    assert note.read_text(encoding="ascii") == "operator's own", "a file that is not a record"
    assert (kept / "Ordinary.json").is_file(), "a subdirectory beside the records"
    assert caplog.records == [], "skipping a non-record is not a failure to report"


# ----- a store the app cannot reach -----


def test_a_store_that_cannot_be_searched_refuses_instead_of_reading_as_free(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """This asserted ``False`` here for the whole of the feature's life. It refuses now.

    The two answers ``origin_recorded`` and ``read_trash_origin`` give still
    AGREE on an unsearchable store, and they still agree harmlessly in other
    ways — a symlink LOOP at the key with a healthy store, where ``exists()``
    swallows ELOOP and the read logs and returns ``None``, measured in
    :func:`test_a_symlink_loop_at_the_key_reads_as_free_on_both_sides`. What
    made THIS agreement a residual is that a record really is on disk and the
    name was handed out anyway: the allocator gave it to a second folder whose
    row would later read the FIRST folder's origin and offer to move it there.

    The residual is closed by refusing the delete (owner ruling,
    ``decisions.md`` 28), not by answering "occupied" — that was measured to
    leave the allocator's loop with no exit at all, 111,939 candidates in one
    second and still climbing. The refusal is raised from BOTH ends: every mover
    asks ``require_usable_store`` before it allocates, and this arm closes the
    window after that check, where a ``/data`` mount drops or a chmod lands
    mid-request. This test is the second of the two, which is why it drives the
    predicate directly rather than a mover.

    ``read_trash_origin`` is deliberately unchanged and asserted unchanged
    below: the restore and Empty sides run AFTER irreversible work, so they keep
    swallow-and-degrade. The ruling is about Delete.

    Skipped as root, where the mode bit denies nothing and the test would report
    green having exercised no fault at all. CI's pytest job runs as "runner".
    """
    if os.getuid() == 0:
        pytest.skip("running as root: an unsearchable directory denies nothing")
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    write_trash_origin(origins, "Dummy", origin="/music/Portishead/Dummy", moved="folder")
    origins.chmod(0o600)  # present, listable, not searchable: stat on a child is EACCES

    try:
        with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
            with pytest.raises(TrashOriginsStoreUnusableError) as ei:
                origin_recorded(origins, "Dummy")
            assert read_trash_origin(origins, "Dummy") is None
    finally:
        origins.chmod(0o700)

    assert "trash-origins" in str(ei.value), "the refusal has to name the store"
    assert str(origins) not in str(ei.value), "...and must not leak its absolute path"
    assert any("so a delete was refused" in r.getMessage() for r in caplog.records), (
        "the absolute path belongs in the log, and the log line is what carries it"
    )
    assert any(str(origins) in r.getMessage() for r in caplog.records)
    # ...and the record really was there all along, which is what the name the
    # allocator used to hand out was still spoken for by.
    assert read_trash_origin(origins, "Dummy") is not None


def test_the_store_refusal_covers_EPERM_as_well_as_EACCES(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second errno in ``_STORE_CLASS_ERRNOS``, which no filesystem here produces.

    The set is a pair — EACCES and EPERM — and the test above only reaches the
    first: a mode bit gives EACCES, and EPERM off this lookup wants something
    this suite cannot stage locally (a mandatory-access-control layer, or a
    filesystem the container is not allowed to traverse regardless of the mode).
    Injected rather than skipped, because "dropped from the set" is otherwise a
    change no test can see and the arm it belongs to is a REFUSAL: with EPERM
    out, this store reads as free and the allocator hands out a name whose
    record is on disk.

    ``Path.exists`` is the exact call ``origin_recorded`` makes, so the stub
    puts the errno where the real one would arrive.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    write_trash_origin(origins, "Dummy", origin="/music/Portishead/Dummy", moved="folder")

    def _eperm(_self: Path) -> bool:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(Path, "exists", _eperm)

    with pytest.raises(TrashOriginsStoreUnusableError) as ei:
        origin_recorded(origins, "Dummy")

    assert "cannot be read" in str(ei.value), "the same sentence the EACCES half gets"
    assert str(origins) not in str(ei.value), "...and no absolute path in it either"


def test_a_store_that_cannot_be_written_refuses_before_anything_moves(tmp_path: Path) -> None:
    """A READ-ONLY store is the same class of fault one permission bit along.

    Measured at tip: mode 0500 reads perfectly, so every check written against
    "can it be read" passes — and then the record write fails and is swallowed,
    and the origin is lost in silence. The ruling's word is "usable", so the
    write is probed too, and with the real syscall rather than ``os.access``:
    ``os.access`` answers for the REAL uid and cannot see a read-only MOUNT,
    which is the deployment shape this fault actually arrives in.

    Skipped as root for the usual reason, and the FILE-at-store shape carries
    the root-proof half of this invariant
    (``test_trash_origin_record.py::test_a_husk_is_refused_when_the_store_is_not_a_folder``).
    """
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only directory denies nothing")
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    origins.chmod(0o500)

    try:
        with pytest.raises(TrashOriginsStoreUnusableError) as ei:
            require_usable_store(origins)
    finally:
        origins.chmod(0o700)

    assert "cannot be written" in str(ei.value)
    assert list(origins.iterdir()) == [], "the write probe must leave nothing behind"


def test_a_store_that_was_never_created_is_created_rather_than_refused(tmp_path: Path) -> None:
    """The shape a fresh install is in, and the one a naive check would refuse.

    Absence is not a fault: no deployment has an origins directory until its
    first delete, and the atomic writer has always created it. A check that read
    ENOENT as "unusable" would refuse the first delete on every new install —
    the twin of ``test_clearing_a_store_that_was_never_created_is_silent``
    above, on the write side.
    """
    origins = tmp_path / "never-created" / "trash-origins"

    require_usable_store(origins)

    assert origins.is_dir()
    assert list(origins.iterdir()) == [], "the probes must leave nothing behind"


def test_a_store_that_cannot_be_created_refuses(tmp_path: Path) -> None:
    """Absent is fine; absent and UNCREATABLE is not, and the two look alike.

    Root-proof: the parent is a regular FILE, so ``mkdir`` answers ENOTDIR for
    any uid. The mode-bit spelling of the same shape (a parent at 0500) is the
    one that would pass vacuously as root, which is why this stages the other.
    """
    parent = tmp_path / "data"
    parent.write_bytes(b"not a directory")

    with pytest.raises(TrashOriginsStoreUnusableError) as ei:
        require_usable_store(parent / "trash-origins")

    assert "is not a usable folder" in str(ei.value)
    assert "Not a directory" in str(ei.value)


def test_a_symlink_loop_at_the_key_reads_as_free_on_both_sides(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A healthy store where the two AGREE anyway, and nothing is at stake.

    Written because the test above used to open "everywhere else the two
    differ", and this is a counter-example with no fault injected and no mode
    bit: a link pointing at itself makes ``Path.exists`` answer False (it
    ignores ELOOP) while ``read_text`` raises it, so the allocator is told the
    name is free and the reader logs an unusable record. That is the same PAIR
    of answers as the unsearchable store and none of the consequence, because
    there is no record here for a second folder to inherit.

    Also the last of the file-content shapes ``delete_trash_origin`` is measured
    against: a link the store cannot follow still has to go, and it goes as a
    link rather than as whatever it points at.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    key = origin_file(origins, "Dummy")
    key.symlink_to(key)

    with caplog.at_level(logging.WARNING, logger="app.beets.trash_origins"):
        assert read_trash_origin(origins, "Dummy") is None
        assert origin_recorded(origins, "Dummy") is False
        delete_trash_origin(origins, "Dummy")  # must return, not raise

    assert any("present but unusable" in r.getMessage() for r in caplog.records)
    # The allocator's "could not tell" warning names what reaches its OSError
    # arm; a loop at the key is not among them, because ``Path.exists`` absorbs
    # ELOOP. This pins the warning's text against the claim.
    assert not any("could not tell" in r.getMessage() for r in caplog.records)
    assert not key.is_symlink(), "the loop was left behind, holding its name"


# ----- the write path's own %r, the third of three -----


def test_a_failed_write_cannot_forge_a_log_line_through_the_entry_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``%r`` on the entry name, pinned exactly as its two siblings are.

    The other two are ``_warn_unusable`` (the record path) and ``trash_manage``'s
    failed-undo line; this one had no test, so ``%r`` -> ``%s`` survived here
    while being killed at both of them. A Trash entry's name comes from the
    album's own tags and ``_trash_container_name`` neutralises separators, NUL
    and U+FFFD but no other control character, so a newline or an ANSI escape
    in an ``albumartist`` reaches this line — the line an operator reads when a
    delete has just lost its origin. Interpolated raw, a crafted album name
    writes whatever it likes into the server log at exactly that moment.

    The write is failed structurally: a regular FILE where the store's directory
    belongs, so ``write_atomic_bytes``'s ``mkdir(parents=True, exist_ok=True)``
    raises ``FileExistsError`` before a byte is written. That needs no ``chmod``,
    so it cannot quietly pass by failing nothing when the suite runs as root.

    It is NOT the read-only ``/data`` case, and must not be described as one:
    there the origins directory already exists, the ``exist_ok=True`` mkdir
    succeeds, and the failure lands one step later at the temp file's
    ``os.open`` (measured: ``PermissionError`` on the ``.tmp`` name). Both land
    in the same ``except Exception``, and this test is about what that handler
    does with the NAME, so the fixture that runs everywhere is the right one.
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
