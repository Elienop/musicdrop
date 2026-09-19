"""The album detail says when some of the album's files are not in the library.

Slice 5 of the import rebuild. Measured through the real in-process import
(``test_import_incremental_e2e.test_a_stop_during_placement_leaves_rows_naming_the_download``):
a stop during placement leaves the placed rows on library paths and the unplaced
rows on the download path, stored absolute, with no state file and no history
entry. The rows are the only record, so the detail endpoint reports the fact and
names one folder.

``holds_every_track`` is what decides whether the app may offer "add that folder
again": only when every track row names a file in that one folder does beets
absorb the old rows itself, with no duplicate question and nothing moved to
Trash (measured in the e2e file, all three file operations). A straddle or a
multi-folder album gets the fact alone.

The same fact is true, by design, for an ``in_place`` import and for rows left in
a Trash outside the music folder. An edited ``directory:`` does NOT produce it —
see ``test_an_edited_directory_keeps_a_whole_album_whole``.
"""

from __future__ import annotations

import errno
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from beets import context
from beets.library import Item, Library
from fastapi.testclient import TestClient

from app.api.albums import get_library
from app.beets.library import _require_id, get_album_detail
from app.main import app
from app.models.album import OutsideLibrary
from tests.conftest import beets_dir_for, build_library, make_test_handle

#: The ``os`` reads the no-disk pin records. Not exhaustive: ``os.getcwd`` (what
#: ``abspath`` calls on a relative row) and builtin ``open`` are outside it.
#: Recorded, not blocked: ``abspath`` -> ``realpath`` calls ``lstat``/``readlink``
#: and swallows their errors, so a raising stub cannot catch it — counting can.
_DISK_READS = ("stat", "lstat", "readlink", "scandir", "listdir", "access", "open")


def _library(tmp_path: Path, name: str = "music") -> Library:
    music = tmp_path / name
    music.mkdir(exist_ok=True)
    return build_library(str(tmp_path / "library.db"), str(music))


def _album(lib: Library, *paths: bytes) -> int:
    """One album whose item rows name ``paths``, in order."""
    items = []
    for i, raw in enumerate(paths, start=1):
        item = Item(album="Alb", albumartist="Art", title=f"T{i}", track=i)
        item.path = raw
        items.append(item)
    album = lib.add_album(items)
    return _require_id(album.id)


def _inside(lib: Library, name: str) -> bytes:
    return os.path.join(lib.directory, os.fsencode(name))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    lib = _library(tmp_path)
    handle = make_test_handle(lib, beets_dir_for(tmp_path))
    app.dependency_overrides[get_library] = lambda: handle
    client = TestClient(app)
    client.lib = lib  # type: ignore[attr-defined]  # the test needs the rows
    yield client
    app.dependency_overrides.clear()


def _outside(lib: Library, album_id: int) -> OutsideLibrary | None:
    detail = get_album_detail(lib, album_id)
    assert detail is not None
    return detail.outside_library


def test_an_album_whose_files_are_all_in_the_library_says_nothing(tmp_path: Path) -> None:
    """The control: a whole album carries nothing."""
    lib = _library(tmp_path)
    aid = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"), _inside(lib, "Art/Alb/02 T2.mp3"))
    assert _outside(lib, aid) is None


def test_an_album_wholly_in_one_outside_folder_may_be_added_again(tmp_path: Path) -> None:
    """The one shape the app offers the remedy for.

    Every row in one folder is what makes beets absorb the album itself on a
    re-import — no duplicate question, nothing to Trash. Measured end to end in
    ``test_the_offered_remedy_finishes_the_album_and_trashes_nothing``.
    """
    lib = _library(tmp_path)
    download = tmp_path / "downloads" / "okc"
    aid = _album(
        lib,
        os.fsencode(str(download / "01 T1.mp3")),
        os.fsencode(str(download / "02 T2.mp3")),
    )
    assert _outside(lib, aid) == OutsideLibrary(folder=str(download), holds_every_track=True)


def test_a_straddling_album_names_the_folder_but_withholds_the_remedy(tmp_path: Path) -> None:
    """One row placed, one still on the download: the folder is not the whole album.

    The folder named is the one really holding the outside file, not the library
    folder the placed row sits in.
    """
    lib = _library(tmp_path)
    download = tmp_path / "downloads" / "okc"
    aid = _album(
        lib,
        _inside(lib, "Art/Alb/01 T1.mp3"),
        os.fsencode(str(download / "02 Track 2.flac")),
    )
    assert _outside(lib, aid) == OutsideLibrary(folder=str(download), holds_every_track=False)


def test_a_multi_folder_album_names_one_folder_and_withholds_the_remedy(tmp_path: Path) -> None:
    """A multi-disc download: every row outside, but in two folders.

    Adding just the named disc and answering Replace takes the OTHER disc's files
    to Trash (security seat H-1), so the remedy is withheld. The folder named
    still really holds an outside file.
    """
    lib = _library(tmp_path)
    okc = tmp_path / "downloads" / "okc"
    aid = _album(
        lib,
        os.fsencode(str(okc / "CD1" / "01 T1.mp3")),
        os.fsencode(str(okc / "CD2" / "01 T3.mp3")),
    )
    assert _outside(lib, aid) == OutsideLibrary(folder=str(okc / "CD1"), holds_every_track=False)


def test_an_in_place_album_outside_the_library_may_be_added_again(tmp_path: Path) -> None:
    """Intended: an ``in_place`` import files nothing, so every row sits in one
    outside folder. Adding it again is measured harmless — the album ends filed
    inside the library, whole, nothing in Trash
    (``test_adding_an_in_place_albums_folder_again_files_it_and_loses_nothing``)."""
    lib = _library(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "Alb"
    aid = _album(lib, os.fsencode(str(elsewhere / "01 T1.mp3")))
    assert _outside(lib, aid) == OutsideLibrary(folder=str(elsewhere), holds_every_track=True)


def test_rows_left_in_a_trash_outside_the_music_folder_are_reported(tmp_path: Path) -> None:
    """Documented, not special-cased.

    A Delete whose Trash move worked but whose row removal failed leaves rows in
    ``<beets_dir>/trash`` — outside the music folder by default. The stated fact
    is still true there, so the field answers it.
    """
    lib = _library(tmp_path)
    trashed = tmp_path / "data" / "trash" / "Art - Alb"
    aid = _album(lib, os.fsencode(str(trashed / "01 T1.mp3")))
    # One folder holding every row, so the remedy IS offered on a Trash entry —
    # security L-1, a residual in BACKLOG. Slice 7's import-start refusal flips
    # this line rather than discovering a silence.
    assert _outside(lib, aid) == OutsideLibrary(folder=str(trashed), holds_every_track=True)


def test_a_sibling_folder_sharing_the_librarys_name_as_a_prefix_is_outside(
    tmp_path: Path,
) -> None:
    """``<music>-inbox`` is not inside ``<music>``.

    A ``startswith`` containment test calls it inside — no notice in exactly the
    stopped-import case, and the edit move phase would treat its files as
    movable (code seat F3).
    """
    lib = _library(tmp_path)
    sibling = Path(os.fsdecode(lib.directory) + "-inbox") / "Art" / "Alb"
    aid = _album(lib, os.fsencode(str(sibling / "01 T1.mp3")))
    assert _outside(lib, aid) == OutsideLibrary(folder=str(sibling), holds_every_track=True)


def test_the_folder_shown_is_the_path_the_predicate_judged(tmp_path: Path) -> None:
    """A ``..`` in a row beets stores verbatim is normalised on BOTH sides.

    A row outside the music dir is stored as written, so its ``..`` survives into
    the read. The whole object is asserted, not just ``folder``: comparing the
    raw dirname on the ``holds_every_track`` side would answer False here while
    ``folder`` still looked right (security seat L-2, W3).
    """
    lib = _library(tmp_path)
    aid = _album(lib, os.fsencode(str(tmp_path / "x")) + b"/../dl/01.mp3")
    assert _outside(lib, aid) == OutsideLibrary(folder=str(tmp_path / "dl"), holds_every_track=True)


def test_an_edited_directory_keeps_a_whole_album_whole(tmp_path: Path) -> None:
    """Editing ``directory:`` does NOT make a whole album read as outside.

    beets stores an inside row RELATIVE (``normalize_path_for_db``), so the row
    follows the new directory. Pinned because the opposite was claimed beside the
    field for one round (code seat F2); it is true of the absolute-path era only.
    """
    old, new = tmp_path / "music-old", tmp_path / "music-new"
    old.mkdir()
    new.mkdir()
    db = str(tmp_path / "library.db")
    lib = build_library(db, str(old))
    aid = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"))
    stored = sqlite3.connect(db).execute("select path from items").fetchall()
    assert stored == [(b"Art/Alb/01 T1.mp3",)], "the row was not stored relative"

    moved = build_library(db, str(new))
    assert _outside(moved, aid) is None


def test_the_answer_does_not_depend_on_an_inherited_music_dir_context(tmp_path: Path) -> None:
    """The read binds beets' music dir itself.

    beets stores an inside-library row RELATIVE and expands it through a
    ``ContextVar`` a FastAPI threadpool thread does not inherit. Unbound and
    unexpanded, every row of a whole album would read as outside.
    """
    lib = _library(tmp_path)
    aid = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"))
    with context.music_dir(b""):
        assert _outside(lib, aid) is None


def test_the_containment_question_records_no_filesystem_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LEXICAL answer makes none of the ``os`` calls in ``_DISK_READS``, so an
    unmounted library answers the same as a mounted one instead of raising.

    Each is RECORDED and still performed; the list must be empty. A raising stub
    would not do: ``realpath`` swallows the errors its ``lstat`` raises and
    answers anyway (code seat F4). Measured: the pristine read makes zero.

    Narrowed to the lexically-INSIDE album on 2026-09-19. The outside branch now
    asks a second, physical opinion (``fsutil.is_in_library_source``), which does
    read the disk — the half below pins that it still cannot RAISE, which is the
    property the unmounted library needed.
    """
    lib = _library(tmp_path)
    inside_id = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"))
    outside_id = _album(lib, os.fsencode(str(tmp_path / "dl" / "Alb" / "01 T1.mp3")))

    seen: list[str] = []
    for name in _DISK_READS:
        real = getattr(os, name)

        def spy(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
            seen.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(os, name, spy)

    assert _outside(lib, inside_id) is None
    assert seen == []

    # The outside branch: reads are expected, an exception is not.
    outside = _outside(lib, outside_id)
    assert outside is not None
    assert outside.folder == str(tmp_path / "dl" / "Alb")


def test_the_second_opinion_cannot_raise_on_an_unreadable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unmounted-library property, now that the outside branch reads the disk.

    Every ``_DISK_READS`` call raises ``OSError(ESTALE)`` — what a dropped NFS
    mount answers. ``is_in_library_source`` swallows each one and answers False,
    so the notice still renders instead of 500ing.
    """
    lib = _library(tmp_path)
    outside_id = _album(lib, os.fsencode(str(tmp_path / "dl" / "Alb" / "01 T1.mp3")))

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.ESTALE, "Stale file handle")

    for name in _DISK_READS:
        monkeypatch.setattr(os, name, boom)

    outside = _outside(lib, outside_id)
    assert outside is not None
    assert outside.folder == str(tmp_path / "dl" / "Alb")


def test_an_album_reached_through_a_symlinked_root_is_not_outside(tmp_path: Path) -> None:
    """The alias spelling of the SAME files, measured by the security seat.

    ``directory:`` is the real music dir; the rows name it through a symlink
    pointing at it (a beets CLI add or an ``in_place`` import through the alias
    produces exactly this). The lexical ``commonpath`` calls them outside, and
    the page then offers "add that folder again" on an album that is already
    filed — re-importing and, with the starter config's ``import.write: yes``,
    re-tagging the files on disk.
    """
    lib = _library(tmp_path)
    real_music = Path(os.fsdecode(lib.directory))
    (real_music / "Art" / "Alb").mkdir(parents=True)
    (real_music / "Art" / "Alb" / "01 T1.mp3").write_bytes(b"\x00")
    alias = tmp_path / "alias"
    alias.symlink_to(real_music, target_is_directory=True)

    album_id = _album(lib, os.fsencode(str(alias / "Art" / "Alb" / "01 T1.mp3")))

    from app.beets.library import _inside_library

    album = lib.get_album(album_id)
    assert album is not None
    (item,) = list(album.items())
    assert _inside_library(lib, item) is False, "the lexical predicate must miss it"
    assert _outside(lib, album_id) is None


def test_a_relative_row_answers_instead_of_raising(tmp_path: Path) -> None:
    """``os.path.abspath`` is what lets ``commonpath`` take an unexpanded row.

    Mixing a relative and an absolute path raises ``ValueError`` there, which
    would 500 the endpoint. The ANSWER is only meaningful with beets' music dir
    bound (the test above); this pins that a caller who forgets still gets one.
    """
    from app.beets.library import _inside_library

    lib = _library(tmp_path)
    assert _inside_library(lib, SimpleNamespace(path=b"Art/Alb/01 T1.mp3")) is False


def test_a_row_with_no_path_names_no_folder(tmp_path: Path) -> None:
    """A NULL/empty path row names no folder a user could act on, so it is not one."""
    lib = _library(tmp_path)
    aid = _album(lib, b"", _inside(lib, "Art/Alb/02 T2.mp3"))
    assert _outside(lib, aid) is None


def test_a_pathless_row_beside_an_outside_row_withholds_the_remedy(tmp_path: Path) -> None:
    """Conservative: a row that names nothing is not a row in the named folder."""
    lib = _library(tmp_path)
    download = tmp_path / "downloads" / "okc"
    aid = _album(lib, b"", os.fsencode(str(download / "01 T1.mp3")))
    assert _outside(lib, aid) == OutsideLibrary(folder=str(download), holds_every_track=False)


def test_the_edit_adapter_shares_this_modules_containment_predicate() -> None:
    """``edit.py``'s move phase and this read name the SAME function object.

    Checks a rebound name, not the absence of every lexical containment helper:
    the adapter has others, for other subjects (``orphans.py``,
    ``import_session.is_in_library_source``, ``trash_origins.py``).
    """
    from app.beets import edit as edit_mod
    from app.beets import library as library_mod

    assert vars(edit_mod)["_inside_library"] is vars(library_mod)["_inside_library"]


def test_an_undecodable_folder_answers_instead_of_500ing(client: TestClient) -> None:
    """Hostile input: a download folder whose name is not valid UTF-8."""
    lib: Library = client.lib  # type: ignore[attr-defined]
    root = os.fsencode(str(Path(os.fsdecode(lib.directory)).parent))
    aid = _album(lib, root + b"/Caf\xe9/01.mp3")

    resp = client.get(f"/api/albums/{aid}")
    assert resp.status_code == 200
    folder = resp.json()["outside_library"]["folder"]
    assert folder.endswith("�")  # the wire sink degraded the byte


def test_a_very_long_folder_answers(client: TestClient) -> None:
    """Hostile input: a folder at the component-length limit."""
    lib: Library = client.lib  # type: ignore[attr-defined]
    long_name = "x" * 255
    root = Path(os.fsdecode(lib.directory)).parent / long_name
    aid = _album(lib, os.fsencode(str(root / "01.mp3")))

    resp = client.get(f"/api/albums/{aid}")
    assert resp.status_code == 200
    assert resp.json()["outside_library"]["folder"] == str(root)


def test_the_field_is_on_the_detail_contract(client: TestClient) -> None:
    """The client is always told, so a whole album answers ``null`` rather than absent."""
    lib: Library = client.lib  # type: ignore[attr-defined]
    aid = _album(lib, _inside(lib, "Art/Alb/01 T1.mp3"))

    body = client.get(f"/api/albums/{aid}").json()
    assert "outside_library" in body
    assert body["outside_library"] is None
