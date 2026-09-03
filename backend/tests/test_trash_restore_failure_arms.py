"""What a move-back restore SAYS and DOES when its file work goes wrong.

Every test here is about the same question, asked from a different failure:
*where are my files now?* The answers used to be composed from which arm raised,
so each one was right for the state that arm was written for and wrong for the
others — a folder still safely in Trash was reported as possibly being in two
places, and a folder stranded at the origin was reported with a sentence that
mentioned neither the library database nor what to do next.

Three properties are pinned throughout:

* the sentence is composed from the DISK, read after the failure
  (``_whereabouts``), so it survives a mechanism change;
* each path sits in its OWN phrase ("in Trash at X" / "at the origin Y"). A
  membership assertion (``str(origin) in message``) passes with the two
  interpolations swapped, and a user following a swapped sentence would import a
  stranger's folder and leave the album where nothing looks for it;
* the origin record is settled from the same observation: kept while anything is
  at the Trash entry, dropped only once that name is really free.

The cross-filesystem (EXDEV) branch gets its own coverage because it is the ONLY
branch the shipped Docker layout ever takes — ``/data`` and ``/music`` are
separate mounts — while every test that builds both trees under one ``tmp_path``
exercises ``os.rename`` and nothing else.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest
from beets import config
from beets.library import Item, Library

from app.beets.trash import trash_album_folder
from app.beets.trash_manage import (
    TrashRestoreIncompleteError,
    _restore_to_origin,
    _return_to_trash,
    restore_album,
)
from app.beets.trash_origins import read_trash_origin
from app.models.trash import RestoreResult
from tests.conftest import build_library, origins_for

SAMPLE = Path(__file__).parent / "fixtures" / "silent.flac"

#: The two ways beets can DECLINE a restore without raising. Named as a type so
#: the parametrised cases below reach ``RestoreResult`` without a cast.
DeclinedReason = Literal["already_in_library", "could_not_restore"]


@pytest.fixture(autouse=True)
def _serial() -> Iterator[None]:
    # Same posture as the other trash suites: single-threaded, and the starter's
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


def _seeded_library(tmp_path: Path, *, folder: str = "Weird Folder") -> Library:
    """The album under test at ``music/<folder>``, plus a bystander album.

    The bystander is not decoration: a restore writes INTO the music library and
    runs behind ``require_library_present``, and a library whose every album is
    missing from disk IS the dropped-share fixture that guard exists to refuse.
    ``folder`` defaults to a name no path template would produce, so a folder
    that lands there can only have come from the origin record.
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


def _trash_the_album(lib: Library, tmp_path: Path) -> Path:
    """Send the album under test to Trash through the real mover, record and all."""
    album = next(a for a in lib.albums() if a.album == "Dummy")
    with lib.transaction():
        return Path(
            trash_album_folder(
                lib,
                album,
                trash_dir=tmp_path / "trash",
                origins_dir=origins_for(tmp_path / "trash"),
            )
        )


def _restore(lib: Library, entry: Path, tmp_path: Path) -> RestoreResult:
    return restore_album(
        lib,
        str(entry),
        trash_dir=tmp_path / "trash",
        origins_dir=origins_for(tmp_path / "trash"),
    )


def _exdev_at(target: Path, monkeypatch: pytest.MonkeyPatch, *, before: Any = None) -> None:
    """Make ``os.rename`` onto ``target`` answer EXDEV, as separate mounts do.

    Only that one destination, and the real ``os.rename`` underneath everything
    else, so beets' own housekeeping is untouched. ``before`` runs first, which
    is how a test puts something at the destination INSIDE the check-then-move
    window: the pre-filter has already looked and the copy has not started.
    """
    real = os.rename

    def fake(src: Any, dst: Any, **kwargs: Any) -> None:
        if Path(os.fsdecode(dst)) == target:
            if before is not None:
                before()
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", fake)


# ----- a restore that failed AND could not be undone -----


def _a_double_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, reason: DeclinedReason
) -> tuple[Library, Path, Path, TrashRestoreIncompleteError]:
    """The import DECLINES (it does not raise) and the Trash entry is retaken.

    This is the arm that used to call ``_return_to_trash`` bare, so the user got
    its one-liner: no word that the album is absent from the library database, a
    first clause ("the source is gone or the Trash entry is occupied") that was
    false — the files were sitting at the origin — and no next step. beets
    declining is the ORDINARY outcome here (a duplicate), which is what makes
    this arm worth as much care as the one where beets raises.

    Something retaking the Trash entry is the window ``_return_to_trash``'s own
    docstring names: a sync client, an ``*arr``, or the user.
    """
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    entry = _trash_the_album(lib, tmp_path)

    def _declines_after_something_retakes_the_trash_entry(
        *_a: object, **_k: object
    ) -> RestoreResult:
        entry.mkdir(parents=True)
        (entry / "stranger.flac").write_bytes(b"\x00")
        return RestoreResult(restored=False, reason=reason)

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import",
        _declines_after_something_retakes_the_trash_entry,
    )
    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _restore(lib, entry, tmp_path)
    return lib, entry, origin, ei.value


@pytest.mark.parametrize("reason", ["already_in_library", "could_not_restore"])
def test_a_declined_import_whose_undo_fails_says_where_the_folder_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: DeclinedReason
) -> None:
    """Both ways beets can decline, because both reach the same stranded state.

    The sentence has to carry all three things the bare undo message did not:
    where the folder is (from the disk), that it never reached the library
    database, and what to do about it. Each path is asserted in its own phrase —
    swap the two interpolations and every membership assertion still passes.
    """
    _, entry, origin, error = _a_double_failure(tmp_path, monkeypatch, reason=reason)

    message = str(error)
    assert f"in Trash at '{entry}'" in message
    assert f"at the origin '{origin}'" in message
    assert "NOT added to the library database" in message
    assert "Compare them before removing either" in message, "a next step, not just a diagnosis"
    assert reason in message, "the import's own answer is still reachable"
    # The composer used to append a full stop to a clause that already ended in
    # one, so the sentence the Trash page renders finished "...again..".
    assert ".." not in message, "one full stop, not two"
    assert message.endswith("."), "and not none either"
    # The claim the sentence makes, measured rather than assumed.
    assert len(list(origin.glob("*.flac"))) == 2
    assert (entry / "stranger.flac").is_file()


def test_a_raised_import_whose_undo_fails_keeps_one_full_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other arm's punctuation, which the decline arm's fix did not reach.

    beets RAISING is the second way into :func:`_undo_failure`, and that arm
    quotes the exception's own message into a sentence of its own. The stop was
    appended there unconditionally, so any error message that ends its own
    sentence — the ordinary shape — rendered ".." in the 500 ``detail`` the
    Trash page shows. Same defect as the one already fixed one function away,
    and the ``".." not in message`` assertions all sat on the other arm.

    The import's own words are asserted too: trimming the double stop must not
    be done by trimming the message.
    """
    lib = _seeded_library(tmp_path)
    entry = _trash_the_album(lib, tmp_path)

    def _raises_after_something_retakes_the_trash_entry(*_a: object, **_k: object) -> RestoreResult:
        entry.mkdir(parents=True)
        (entry / "stranger.flac").write_bytes(b"\x00")
        raise RuntimeError("beets could not read the folder.")

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import",
        _raises_after_something_retakes_the_trash_entry,
    )
    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _restore(lib, entry, tmp_path)

    message = str(ei.value)
    assert "beets could not read the folder." in message, "the import's own words, intact"
    assert ".." not in message, "one full stop, not two"
    assert "Returning it to Trash then failed with:" in message, "and the undo's story after it"
    assert message.endswith("."), "and not none either"


def test_an_import_error_ending_in_a_newline_leaves_no_stray_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same double stop, one character further along, where nothing saw it.

    The test above quotes an error ending "folder." and the composer leaves it
    alone. An error ending "folder.\\n" — a plugin quoting a subprocess's output,
    which keeps its line ending — ended in a NEWLINE, so the last-character test
    said "unfinished" and appended: the sentence the Trash page renders then
    carried a full stop standing on its own, after the break, in the middle of
    the message.

    Asserted as the invariant rather than as one rendering: no full stop in the
    composed message stands after whitespace. A substring assertion on the join
    would pin the two clauses' exact wording instead.
    """
    lib = _seeded_library(tmp_path)
    entry = _trash_the_album(lib, tmp_path)

    def _raises_after_something_retakes_the_trash_entry(*_a: object, **_k: object) -> RestoreResult:
        entry.mkdir(parents=True)
        (entry / "stranger.flac").write_bytes(b"\x00")
        raise RuntimeError("beets could not read the folder.\n")

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import",
        _raises_after_something_retakes_the_trash_entry,
    )
    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _restore(lib, entry, tmp_path)

    message = str(ei.value)
    assert "beets could not read the folder." in message, "the import's own words, intact"
    assert re.search(r"\s\.", message) is None, "no stop standing on its own after whitespace"
    assert ".." not in message, "one full stop, not two"
    assert message.endswith("."), "and not none either"


def test_the_origin_record_survives_a_trash_entry_that_still_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record outlives the failure while ANYTHING is at the Trash name.

    Nothing on disk can separate "a stranger took the name" from "our copy is
    half-removed", and the two want opposite answers. Deleting on the guess costs
    a real Trash row its exact restore permanently; keeping it costs a wrong
    "Exact restore" promise that ``_restore_to_origin`` refuses on the spot,
    because the origin it names is occupied by this very album. The cheap wrong
    answer is the one to take.
    """
    _, entry, _, _ = _a_double_failure(tmp_path, monkeypatch, reason="already_in_library")

    assert read_trash_origin(origins_for(tmp_path / "trash"), entry.name) is not None


def test_the_origin_record_goes_once_the_trash_name_is_really_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same rule, and the message for "neither path".

    The import consumed the folder and declined, so the undo has nothing to move
    back and the entry is gone from Trash: the record now describes nothing and
    its NAME is free, where a leftover would be inherited by the next folder to
    earn it. The sentence must not claim the folder is at either path when it is
    at neither — that is the one state where a confident answer sends the user to
    an empty directory.
    """
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    entry = _trash_the_album(lib, tmp_path)

    def _consumes_the_folder_then_declines(*_a: object, **_k: object) -> RestoreResult:
        shutil.rmtree(origin)
        return RestoreResult(restored=False, reason="already_in_library")

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import", _consumes_the_folder_then_declines
    )
    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _restore(lib, entry, tmp_path)

    message = str(ei.value)
    assert "no longer find the folder at EITHER path" in message
    assert f"in Trash at '{entry}'" in message
    assert f"at the origin '{origin}'" in message
    assert "NOT added to the library database" in message
    assert read_trash_origin(origins_for(tmp_path / "trash"), entry.name) is None


def test_a_failed_undo_says_the_folder_is_at_the_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state the old message ASSERTED, now reached honestly.

    The forward move landed, the import declined, and the undo could not put the
    folder back because the Trash volume stopped accepting writes — a read-only
    remount, a full disk, a permission bit. The entry is gone, the album is at
    the origin, and the sentence has to say so in that order, name the library
    database, and give the one action that finishes the job. Here the record's
    name IS free again, which is the half of the rule this pins: it goes.

    ``chmod`` on the Trash dir rather than a patched mover, so the real
    ``os.rename`` produces the real EACCES; restored in a ``finally`` or the
    ``tmp_path`` teardown cannot clean up. Skipped as root, where the mode bit
    denies nothing and the undo would simply succeed.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    trash = tmp_path / "trash"
    entry = _trash_the_album(lib, tmp_path)

    def _declines_after_trash_goes_read_only(*_a: object, **_k: object) -> RestoreResult:
        trash.chmod(0o500)
        return RestoreResult(restored=False, reason="already_in_library")

    monkeypatch.setattr(
        "app.beets.trash_manage._restore_by_import", _declines_after_trash_goes_read_only
    )
    try:
        with pytest.raises(TrashRestoreIncompleteError) as ei:
            _restore(lib, entry, tmp_path)
    finally:
        trash.chmod(0o700)

    message = str(ei.value)
    assert f"no longer in Trash at '{entry}'" in message
    assert f"at the origin '{origin}'" in message
    assert "NOT added to the library database" in message
    assert "Importing that folder is what finishes putting the album back" in message
    assert len(list(origin.glob("*.flac"))) == 2, "the sentence's claim, measured"
    assert not entry.exists()
    # The Trash name is free again, so a record left here would be inherited by
    # whatever earns that name next — and it steers a rename().
    assert read_trash_origin(origins_for(trash), entry.name) is None


# ----- a failure BEFORE anything moved -----


def test_a_failure_creating_the_destination_says_nothing_moved(tmp_path: Path) -> None:
    """The music share refuses the ``mkdir``, and nothing has been touched.

    A plain FILE at a parent path component is the deterministic version of the
    EROFS / ENOSPC / EACCES this really hits — the same syscall, the same arm.
    Sharing the move's ``try`` made every one of them report that the folder "may
    now be in BOTH places", sending the user to look for a copy at a path that
    does not exist, while the folder sat untouched in Trash.
    """
    lib = _seeded_library(tmp_path, folder="Portishead/Dummy")
    (tmp_path / "music" / "Artist").write_bytes(b"not a folder")
    origin = tmp_path / "music" / "Artist" / "Album"
    entry = tmp_path / "trash" / "Artist - Album"
    entry.mkdir(parents=True)
    (entry / "01 a.flac").write_bytes(b"\x00")
    origins = origins_for(tmp_path / "trash")

    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _restore_to_origin(lib, entry, origin, trash_dir=tmp_path / "trash", origins_dir=origins)

    message = str(ei.value)
    assert f"could not create the folder '{origin.parent}'" in message, "the real cause"
    assert "The move was not attempted" in message
    assert f"still in Trash at '{entry}'" in message
    assert f"nothing is at the origin '{origin}'" in message
    assert "BOTH" not in message, "nothing moved, so there is no second place to look"
    assert (entry / "01 a.flac").is_file()
    assert not origin.exists()


def test_a_part_way_move_names_each_path_in_its_own_phrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy branch failed mid-copy, so the folder really is in two places.

    The existing suite pins that the sentence SAYS "BOTH places"; this pins that
    it says which is which. The paths are two absolute strings the user acts on,
    and the wrong way round they send someone to delete the complete copy and
    keep the partial one.
    """
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Restored Here"
    entry = tmp_path / "trash" / "Weird Folder"
    entry.mkdir(parents=True)
    (entry / "01 Mysterons.flac").write_bytes(b"\x00")

    def _half_a_copy(src: Any, dst: Any, **_k: Any) -> None:
        os.makedirs(dst)
        (Path(dst) / "01 Mysterons.flac").write_bytes(b"\x00")
        raise OSError(errno.EIO, "Input/output error")

    _exdev_at(origin, monkeypatch)
    monkeypatch.setattr(shutil, "copytree", _half_a_copy)
    origins = origins_for(tmp_path / "trash")

    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _restore_to_origin(lib, entry, origin, trash_dir=tmp_path / "trash", origins_dir=origins)

    message = str(ei.value)
    assert "could not move the folder back out of Trash" in message
    assert f"in Trash at '{entry}'" in message
    assert f"at the origin '{origin}'" in message
    assert "Retrying cannot make it worse" in message
    assert (entry / "01 Mysterons.flac").is_file()
    assert (origin / "01 Mysterons.flac").is_file()


# ----- the undo's own two refusals and its failure arm -----


def test_return_to_trash_names_the_retaken_entry_in_its_own_phrase(tmp_path: Path) -> None:
    """The refusal a user reads through :func:`_undo_failure`.

    ``shutil.move`` onto an existing directory buries the folder inside it, so
    this refuses — and the sentence it refuses with is the operator's only
    pointer at two paths that look alike. It also has to say WHICH of the two
    conditions fired: the old wording offered both ("the source is gone or the
    Trash entry is occupied") when the code already knew.
    """
    origin = tmp_path / "music" / "Weird Folder"
    origin.mkdir(parents=True)
    (origin / "01 a.flac").write_bytes(b"\x00")
    entry = tmp_path / "trash" / "Weird Folder"
    entry.mkdir(parents=True)
    (entry / "stranger.flac").write_bytes(b"\x00")

    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _return_to_trash(origin, entry)

    message = str(ei.value)
    assert f"at the origin '{origin}'" in message
    assert f"Trash entry '{entry}' again" in message
    assert "nothing at the origin" not in message, "the other condition, which did not fire"
    assert (origin / "01 a.flac").is_file()
    assert sorted(p.name for p in entry.iterdir()) == ["stranger.flac"]


def test_return_to_trash_names_each_path_when_the_move_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth message: both pre-filters passed and the move still failed.

    An I/O error, a full Trash volume, a permission bit. The folder is at the
    origin and the user has to be told that in a phrase that cannot be read the
    other way round.
    """
    origin = tmp_path / "music" / "Weird Folder"
    origin.mkdir(parents=True)
    (origin / "01 a.flac").write_bytes(b"\x00")
    entry = tmp_path / "trash" / "Weird Folder"
    entry.parent.mkdir(parents=True)

    def _fails(*_a: object, **_k: object) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr("app.beets.trash_manage.move_no_merge", _fails)
    with pytest.raises(TrashRestoreIncompleteError) as ei:
        _return_to_trash(origin, entry)

    message = str(ei.value)
    assert "the restore did not land" in message
    assert f"at the origin '{origin}'" in message
    assert f"Trash at '{entry}'" in message
    assert "Input/output error" in message, "the syscall's own answer is still there"


# ----- an EMPTY directory at the origin: one answer, on both branches -----


def test_an_empty_directory_at_the_origin_is_replaced_by_the_rename(tmp_path: Path) -> None:
    """An empty folder is not an occupant, and ``os.rename`` already knew.

    A pruning beets and a half-finished sync both leave empty directories where
    an album used to be, so this is the ordinary state of an origin the user
    wants back — and the ``exists`` pre-filter refused it while the move it was
    protecting would have performed it. Nothing can be buried or merged: there
    is nothing in it.
    """
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    entry = _trash_the_album(lib, tmp_path)
    origin.mkdir(parents=True)  # the sync client's leftover

    result = _restore(lib, entry, tmp_path)

    assert result.restored is True
    assert sorted(p.name for p in origin.glob("*.flac")) == [
        "01 Mysterons.flac",
        "02 Sour Times.flac",
    ]
    assert not entry.exists()


def test_an_empty_directory_at_the_origin_is_replaced_on_the_copy_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SAME input, the SAME answer, on the branch the real deployment takes.

    ``copytree`` refuses any destination that exists, so this branch answered
    ``origin_occupied`` where the rename branch restored the album — and the
    deployment that takes it (``/data`` and ``/music`` on separate mounts) takes
    it for EVERY restore, so the divergence was not an edge case there but the
    whole behaviour.
    """
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    entry = _trash_the_album(lib, tmp_path)
    origin.mkdir(parents=True)
    _exdev_at(origin, monkeypatch)

    result = _restore(lib, entry, tmp_path)

    assert result.restored is True
    assert sorted(p.name for p in origin.glob("*.flac")) == [
        "01 Mysterons.flac",
        "02 Sour Times.flac",
    ]
    assert not entry.exists()


def test_the_restore_result_schema_does_not_call_an_empty_folder_occupied(
    tmp_path: Path,
) -> None:
    """The wire model's own words, read against the restore they describe.

    ``RestoreResult``'s docstring IS the description the generated TypeScript
    client carries, and it defined ``origin_occupied`` as "the folder it came
    from exists again" — which is exactly the disk state the two tests above put
    down and then watch the restore GO AHEAD from. The behaviour was pinned and
    the sentence describing it was not, so the contract documented a refusal the
    wire does not make.

    Two halves, neither evidence on its own: a real restore into an empty
    leftover (a wrong description passes it) and the LIVE spec's description (a
    wrong behaviour passes any assertion about the spec).
    ``tests/test_openapi_spec_guard.py`` is not evidence about either — it only
    fires on a dump that was not regenerated.

    The spec half reads the CLAIM, not the word. Asserting ``"empty" in
    description`` pinned an absence: the sentence inverted to "An EMPTY leftover
    folder at the origin IS occupied" — the exact wrong contract this test was
    added for — kept the word and passed (measured on this file). So the
    sentence that mentions an empty folder must also be the one that denies it
    is occupied.
    """
    from app.main import app

    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    entry = _trash_the_album(lib, tmp_path)
    origin.mkdir(parents=True)

    result = _restore(lib, entry, tmp_path)

    assert result.restored is True, "an empty leftover at the origin is replaced, not refused"
    description = app.openapi()["components"]["schemas"]["RestoreResult"]["description"]
    about_empty = [s for s in description.split(".") if "empty" in s.lower()]
    assert about_empty, (
        "the description defines origin_occupied without ever mentioning an EMPTY folder at"
        f" the origin — this restore answered {result.reason!r} with one there"
    )
    assert any("not occupied" in s.lower() for s in about_empty), (
        "the description mentions an empty folder at the origin without saying it is NOT"
        f" occupied — this restore answered {result.reason!r} with one there: {about_empty}"
    )


def test_a_directory_the_app_cannot_read_is_treated_as_occupied(tmp_path: Path) -> None:
    """:func:`occupied`'s ``except OSError`` arm, which nothing reached.

    A directory the app can stat but not READ — a mode bit, a share that came
    back with different ownership, a fault mid-``scandir`` — leaves "is anything
    in it?" unanswered, and the docstring's promise is that an unanswered
    question counts as occupied. That is the only conservative reading: a
    refusal leaves the album in Trash, while letting the ``OSError`` out turns a
    refusal the user can act on into a 500 from a route whose contract says
    nothing about permissions.

    The directory is EMPTY, so this cannot pass for the ordinary reason: with
    the arm removed, the same fixture would either restore into it (if the read
    succeeded) or raise. Skipped as root, where mode 000 denies nothing.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: mode 000 does not deny a scandir")
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    entry = _trash_the_album(lib, tmp_path)
    origin.mkdir(parents=True)
    origin.chmod(0o000)

    try:
        result = _restore(lib, entry, tmp_path)
    finally:
        origin.chmod(0o700)  # or the tmp_path teardown cannot clean up

    assert result == RestoreResult(restored=False, reason="origin_occupied")
    assert len(list(entry.glob("*.flac"))) == 2, "still in Trash, untouched"


@pytest.mark.parametrize("occupant", ["files", "file", "symlink"])
def test_anything_else_at_the_origin_still_refuses(tmp_path: Path, occupant: str) -> None:
    """The empty-directory rule must not widen into "overwrite whatever is there".

    A non-empty directory would be merged into, a file would be replaced, and a
    SYMLINK would move the album to wherever the link points — a place the user
    never named. All three keep the files in Trash instead.

    What holds the symlink case is NOT this test's business, and saying otherwise
    once made it read as the pin for :func:`occupied`'s ``is_symlink`` arm.
    ``os.rename`` answers ENOTDIR on a link destination and
    :func:`move_no_merge` normalises that to the same ``origin_occupied``, so
    the outcome asserted below survives with that arm dropped — measured, this
    test and ``test_an_empty_directory_at_the_origin_is_replaced_by_the_rename``
    both pass against an ``is_symlink``-less ``occupied``. The arm is a tripwire
    on what ``occupied`` REPORTS to a future caller (without it, "an empty
    directory" silently includes a link to one); its own docstring is where that
    is argued, and it says no test kills it.
    """
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    entry = _trash_the_album(lib, tmp_path)
    if occupant == "files":
        origin.mkdir(parents=True)
        (origin / "someone else.flac").write_bytes(b"\x00")
    elif occupant == "file":
        origin.write_bytes(b"\x00")
    else:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        origin.symlink_to(elsewhere, target_is_directory=True)

    result = _restore(lib, entry, tmp_path)

    assert result == RestoreResult(restored=False, reason="origin_occupied")
    assert len(list(entry.glob("*.flac"))) == 2, "still in Trash, untouched"
    if occupant == "symlink":
        assert list((tmp_path / "elsewhere").iterdir()) == [], "and nothing followed the link"


def test_the_copy_branch_refuses_a_destination_that_appeared_in_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``copytree`` must not be allowed to merge, and nothing pinned that.

    Both existing window tests build Trash and music under one ``tmp_path``, so
    only ``os.rename`` ever runs and ``dirs_exist_ok=True`` on the copy survived
    the whole suite — on the one branch the shipped Docker layout actually takes.
    Merged, the album's files land among a stranger's and the Trash entry is
    removed: the two albums are then inseparable by anything but hand.

    The destination is created INSIDE the check-then-move window, which is the
    only way the copy branch can meet one: the pre-filter has already answered
    "free", and a sync client, an ``*arr`` or the user creates it a moment later.
    """
    lib = _seeded_library(tmp_path)
    origin = tmp_path / "music" / "Weird Folder"
    entry = _trash_the_album(lib, tmp_path)

    def _a_sync_client_gets_there_first() -> None:
        origin.mkdir(parents=True)
        (origin / "stranger.flac").write_bytes(b"\x00")

    _exdev_at(origin, monkeypatch, before=_a_sync_client_gets_there_first)

    result = _restore(lib, entry, tmp_path)

    assert result == RestoreResult(restored=False, reason="origin_occupied")
    assert [p.name for p in origin.iterdir()] == ["stranger.flac"], "nothing was merged in"
    assert sorted(p.name for p in entry.glob("*.flac")) == [
        "01 Mysterons.flac",
        "02 Sour Times.flac",
    ], "the album is still in Trash"
