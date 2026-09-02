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
  recalled, by walking the module's AST for ``logger.*`` calls at this commit:
  seven calls, six of which interpolate something, seven ``%r`` placeholders
  between them. Five of the six are pinned here — the write's warning, the
  delete's failure, the kept-record refusal (both of its placeholders), the
  allocator's "could not tell", and the store sweep — and the sixth,
  ``_warn_unusable``, is pinned in ``test_trash_origin_record.py``
  (``test_an_unusable_record_cannot_forge_a_log_line_through_its_own_filename``).
  The count is a measurement, so re-run the walk rather than trusting this
  sentence after adding a line.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.trash_manage import empty_all, empty_one, list_trashed_albums, restore_album
from app.beets.trash_origins import (
    clear_trash_origins,
    delete_trash_origin,
    origin_file,
    origin_recorded,
    read_trash_origin,
    write_trash_origin,
)
from tests.conftest import build_library

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
    ("label", "payload"),
    [
        ("no name at all", '{"schema": 1, "origin": "/music/A", "moved": "folder"}'),
        ("not an object", '["schema", 1]'),
        ("not JSON", "{ not json at all"),
        ("a name that is not a string", '{"schema": 1, "name": 7, "origin": "/music/A"}'),
    ],
)
def test_a_record_that_does_not_name_another_entry_is_still_dropped(
    tmp_path: Path, label: str, payload: str
) -> None:
    """Only a positively identified STRANGER survives; every doubt still unlinks.

    ``restore_album``'s import arm relies on this: an unreadable record reaches
    ``delete_trash_origin`` precisely because ``read_trash_origin`` collapsed it
    to ``None``, and leaving it behind burns its name forever (the allocator
    reads it as occupied) for a file that steers nothing. So "keep what I cannot
    parse" is the wrong safe side here, and the four shapes below are the ones a
    pre-feature record, a truncated write and a hand edit actually produce.
    """
    origins = tmp_path / "trash-origins"
    origins.mkdir()
    origin_file(origins, "Dummy").write_text(payload, encoding="ascii")

    delete_trash_origin(origins, "Dummy")

    assert not origin_file(origins, "Dummy").exists(), f"{label} must not be left behind"


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

    assert empty_one(str(trash / short_name), origins_dir=origins).removed == 1

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

    result = restore_album(lib, str(trash / short_name), trash_dir=trash, origins_dir=origins)

    assert result.restored is True
    assert (tmp_path / "music" / "Portishead" / "Dummy").is_dir()
    survivor = read_trash_origin(origins, long_name)
    assert survivor is not None, "a restore of one row destroyed another row's record"
    assert survivor.origin == str(tmp_path / "music" / "Long")


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
    name = os.fsdecode(b"Caf\xc3\xa9 \xff \x1b[31m\nCRITICAL:app:all clear")
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
    # ...and the ``%r`` is load-bearing on this line too. ``display_path``
    # neutralises the undecodable byte and NOTHING else, so an ANSI escape and a
    # newline in the same name — both ordinary characters in an ``albumartist``
    # — reach the log unless the interpolation quotes them. Spelled ``%s`` the
    # two assertions above still pass, which is how this site stayed unpinned.
    assert "\x1b" not in caplog.text, "an ANSI escape reached the log"
    assert "\nCRITICAL" not in caplog.text, "a forged log line reached the log"
    assert "\\x1b" in caplog.text  # escaped, not dropped
    assert "\\n" in caplog.text


# ----- ...including a file the JSON parser gives up on -----


def _deeply_nested_json() -> str:
    """A legal-ASCII, legal-JSON file that ``json.loads`` refuses to finish.

    60,000 nested arrays. The depth is not a threshold this suite owns — CPython
    trips its own C recursion limit long before here — so the premise is
    asserted rather than assumed: if the parser ever gets deep enough to swallow
    this, the line below goes red instead of the tests going quietly green.

    The trigger is corruption, a hand edit, or a restored backup on the trusted
    ``/data`` side, not a plant: nothing in ``/music`` can write into this
    directory (see the module docstring of ``app.beets.trash_origins``). What is
    new is not that the file is unusable — the store has always had unusable
    shapes — but that reading it raised AFTER the entry was already destroyed.
    """
    text = "[" * 60_000 + "]" * 60_000
    with pytest.raises(RecursionError):
        json.loads(text)
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

    assert empty_one(str(trash / "Deep"), origins_dir=origins).removed == 1

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

    assert empty_all(trash, origins_dir=origins).removed == 3

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
#: the album's own ``albumartist``/``album`` tags and neutralises path separators
#: and nothing else, so an ANSI escape or a newline in a tag arrives here intact.
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

    This line runs at the exact moment the store's stated residual is created:
    the name reads as free although a record may be sitting on it, so the
    allocator can hand it to a second folder that will later read the first
    folder's origin. It is the only trace of that, and it is reached with a name
    the album's own tags can produce.

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
    with pytest.raises(OSError) as raw:
        origin_file(origins, _FORGED_ENTRY_NAME).exists()
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


# ----- a store the app cannot reach -----


def test_a_store_that_cannot_be_reached_reads_as_free_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``origin_recorded`` and ``read_trash_origin`` AGREE here, and that is the residual.

    The two DIFFER on a record that is present, reachable and refused — the five
    shapes ``origin_recorded`` lists — and there "occupied" is the safe side.
    They agree in plenty of harmless ways too, including one that looks like this
    case and is not: a symlink LOOP at the key with a healthy store, where
    ``exists()`` swallows ELOOP and answers False while the read logs and returns
    ``None`` (measured in
    :func:`test_a_symlink_loop_at_the_key_reads_as_free_on_both_sides`).

    What makes THIS agreement the residual is not the agreement, it is that a
    record really is on disk and the name is handed out anyway. An origins
    directory that is present but unsearchable takes the safety net away:
    ``exists()`` raises ``EACCES``, the name reads as free, and the allocator can
    hand it to a second folder whose row will later read the FIRST folder's
    origin. Answering "occupied" instead would leave the allocator's loop with no
    exit (every candidate occupied), so the honest answer is the unsafe one and
    this log line is its only trace.

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
            recorded = origin_recorded(origins, "Dummy")
            assert read_trash_origin(origins, "Dummy") is None
    finally:
        origins.chmod(0o700)

    assert recorded is False, "the allocator must not be told 'occupied' here — see the docstring"
    assert any(
        "could not tell whether a Trash origin record exists" in r.getMessage()
        for r in caplog.records
    )
    # ...and the record really was there all along, so what the allocator would
    # hand out is a name that is still spoken for.
    assert read_trash_origin(origins, "Dummy") is not None


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
    assert not key.is_symlink(), "the loop was left behind, holding its name"


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
