"""beets' ``PathQuery``, the surface Delete's retry arm and Empty both lean on.

Both sides ask "does the library still list a file inside this folder?" through
it (``delete._all_rows_are_in_trash``, ``trash_manage._listed_entries``) rather
than through a prefix compare of their own — three rounds of review found faults
in the hand-rolled version, and the answers have to agree or an entry Empty
refuses is one the retry arm does not recognise.

Five properties are load-bearing, so a beets bump that changes any of them fails
here rather than in a route: the constructor's ``(field, bytes)`` shape, the
DIRECTORY-prefix arm's separator boundary, relative DB rows under ``directory:``,
case sensitivity decided by probing the filesystem, and a pattern that KEEPS the
spelling it was given (``util.normpath`` is ``abspath``, not ``realpath``) — the
last is why one caller asks with several spellings rather than one. The SQL arm
(``lib.items``) and the Python arm (``match``) are asserted to agree, because the
two callers use different ones.

Measured against beets 2.13.1.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from beets.dbcore.query import PathQuery
from beets.library import Item, Library


def _library(tmp_path: Path) -> Library:
    music = tmp_path / "music"
    music.mkdir()
    return Library(str(tmp_path / "library.db"), directory=str(music))


def _row(lib: Library, path: str, *, title: str = "T1") -> Item:
    item = Item(album="Alb", albumartist="Art", artist="Art", title=title, track=1)
    item.path = os.fsencode(path)
    lib.add_album([item])
    return item


def test_the_directory_arm_matches_what_is_inside_and_nothing_beside_it(tmp_path: Path) -> None:
    """The prefix carries a separator, so a ``trash-old`` sibling is outside.

    This is the property both callers' "is this row in Trash?" rests on, and the
    one a bare ``startswith`` gets wrong.
    """
    lib = _library(tmp_path)
    trash = tmp_path / "trash"
    inside = _row(lib, str(trash / "Art - Alb" / "01 T1.mp3"))
    beside = _row(lib, str(tmp_path / "trash-old" / "Art - Alb" / "02 T2.mp3"), title="T2")

    with lib.music_dir_context():
        query = PathQuery("path", os.fsencode(str(trash)))

        assert query.match(inside)
        assert not query.match(beside)
        assert [i.title for i in lib.items(query)] == ["T1"], "the SQL arm agrees"


def test_a_row_naming_the_path_itself_matches(tmp_path: Path) -> None:
    """A Trash entry that IS a loose file is one question, not a second one."""
    lib = _library(tmp_path)
    loose = tmp_path / "trash" / "01 Only Copy.mp3"
    row = _row(lib, str(loose))

    with lib.music_dir_context():
        assert PathQuery("path", os.fsencode(str(loose))).match(row)


def test_a_row_inside_the_music_dir_is_stored_and_matched_RELATIVE(tmp_path: Path) -> None:
    """beets' DB representation, which is why neither caller joins paths itself.

    A Trash inside ``directory:`` is the supported layout where this fires: the
    row on disk is relative and the query pattern is made relative to match it.
    """
    lib = _library(tmp_path)
    trash = tmp_path / "music" / ".trash"
    row = _row(lib, str(trash / "Art - Alb" / "01 T1.mp3"))

    with lib.transaction() as tx:
        (stored,) = tx.query("SELECT path FROM items")
    assert stored[0] == os.path.join(b".trash", b"Art - Alb", b"01 T1.mp3"), "beets stored it"

    with lib.music_dir_context():
        query = PathQuery("path", os.fsencode(str(trash)))
        assert query.pattern == b".trash", "and the pattern went relative too"
        assert query.match(row)
        assert len(list(lib.items(query))) == 1, "the SQL arm agrees"


def test_case_sensitivity_is_decided_by_probing_the_filesystem(tmp_path: Path) -> None:
    """On case-SENSITIVE storage two spellings are two entries, and must stay so.

    The casefolding half is measured on a real ``casefold=utf8-12.1.0`` tmpfs by
    ``test_empty_refuses_a_row_that_spells_the_entry_in_another_case``; this is
    the control, and it is also what stops a differently-named album from being
    refused for ever with a remedy that clears something else.
    """
    trash = tmp_path / "trash"
    (trash / "Art - Alb").mkdir(parents=True)
    other = trash / "ART - ALB"
    if other.exists():  # pragma: no cover - a casefolding tmpdir
        pytest.skip("this filesystem folds case, so there is only one entry here")
    lib = _library(tmp_path)
    row = _row(lib, str(trash / "Art - Alb" / "01 T1.mp3"))

    with lib.music_dir_context():
        query = PathQuery("path", os.fsencode(str(other)))
        assert query.case_sensitive, "beets probed the filesystem and found it sensitive"
        assert not query.match(row)


def test_the_pattern_keeps_a_symlink_spelling_rather_than_resolving_it(tmp_path: Path) -> None:
    """``util.normpath`` is ``abspath`` + ``normpath``, and never ``realpath``.

    This is why ``protected._trash_spellings`` exists: the default Trash is used
    UNRESOLVED, so its mover writes rows through the link, while a configured one
    is resolved at the source and writes rows through the real path. One pattern
    answers for one of those, and the other entry is emptied with the album's
    only copy in it. Should a bump start resolving, this is NOT a safe no-op: the
    code seat simulated the drift and **five door tests in
    ``tests/test_trash_api.py`` fail**, because a link-spelled row stops matching
    the pattern it was written under. This file is where that has to be noticed
    first.
    """
    lib = _library(tmp_path)
    real = tmp_path / "bigdisk-trash"
    real.mkdir()
    link = tmp_path / "trash"
    link.symlink_to(real)
    row = _row(lib, str(link / "Art - Alb" / "01 T1.mp3"))

    with lib.music_dir_context():
        through_link = PathQuery("path", os.fsencode(str(link)))
        resolved = PathQuery("path", os.fsencode(str(real)))

        assert through_link.pattern == os.fsencode(str(link)), "the link spelling was kept"
        assert through_link.match(row)
        assert not resolved.match(row), "the same directory, and no match"
        assert [i.title for i in lib.items(resolved)] == [], "the SQL arm agrees"
