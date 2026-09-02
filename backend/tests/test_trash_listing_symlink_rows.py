"""The listing row for a Trash entry that is itself a SYMLINK.

Nothing hostile is needed to get one there: ``trash._album_root`` is
``dirname(item.path)``, so an album whose own folder is a symlink into another
volume is trashed AS a symlink, because ``shutil.move`` recreates the link and
unlinks the original. Only the LINK ever moves — the album's files stay on the
volume they were on.

``trash._record_origin`` declines to write a record for such an entry, on
purpose, and the row then fell through to the generic no-record note. Every
claim in that sentence is false here, which is what this file pins: nothing
failed and the log says nothing, the row is not old, and "Restoring re-imports
it" is not on offer at all — ``resolve_trash_child`` refuses a child that
resolves outside Trash, so Restore answers 404 and the note describes an action
the app will not take.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.beets.trash_manage import list_trashed_albums, resolve_trash_child
from tests.conftest import origins_for


def _trash_with_a_symlinked_entry(tmp_path: Path) -> tuple[Path, Path]:
    """A Trash dir holding one ordinary entry and one symlinked one."""
    trash, elsewhere = tmp_path / "trash", tmp_path / "elsewhere"
    trash.mkdir()
    elsewhere.mkdir()
    (elsewhere / "01 a.flac").write_bytes(b"\x00")
    (tmp_path / "music").mkdir()
    (trash / "Real Album").mkdir()
    (trash / "Symlinked Album").symlink_to(elsewhere, target_is_directory=True)
    return trash, elsewhere


def _rows(trash: Path, tmp_path: Path) -> dict[str, tuple[str, str | None, str | None]]:
    return {
        row.folder: (row.restore_mode, row.restore_note, row.origin)
        for row in list_trashed_albums(
            trash, origins_dir=origins_for(trash), music_dir=str(tmp_path / "music")
        )
    }


def test_a_symlinked_row_says_what_restore_will_really_do(tmp_path: Path) -> None:
    """The note has to match the route, and it has to say where the files are.

    The user's next action here is not "wait", "re-point the library" or "put it
    back by hand" — it is "go to the volume the link points at", and the reason
    that action exists at all is the good news this row carries: the album's
    files were never moved. The generic no-record sentence sent them to look at
    the folder's age or at the server log instead, for an event that never
    happened.
    """
    trash, _ = _trash_with_a_symlinked_entry(tmp_path)

    mode, note, origin = _rows(trash, tmp_path)["Symlinked Album"]

    assert mode == "refused", "no per-row route will act on this at all"
    assert note is not None
    assert "link to a folder on another volume" in note, "why it cannot be restored"
    assert "will not restore it" in note, "and that MusicDrop will not try"
    assert "files were never moved" in note, "where the album actually is"
    assert "before origins were recorded" not in note, "neither record cause applies"
    assert "writing that record failed" not in note
    assert origin is None


def test_the_symlinked_row_matches_what_the_restore_route_will_do(tmp_path: Path) -> None:
    """The note and the route have to agree, so both are asserted together.

    ``resolve_trash_child`` is the guard the endpoint maps to its 404: it
    resolves the child and refuses anything landing outside Trash. A note
    promising a re-import while this refuses is the mismatch the row used to
    ship. The ordinary sibling is asserted in the same breath, so a change that
    silenced the note by breaking the whole listing would be visible.
    """
    trash, _ = _trash_with_a_symlinked_entry(tmp_path)
    rows = _rows(trash, tmp_path)

    with pytest.raises(ValueError):
        resolve_trash_child(trash, "Symlinked Album")

    assert resolve_trash_child(trash, "Real Album") == (trash / "Real Album").resolve()
    real_mode, real_note, _ = rows["Real Album"]
    assert real_mode == "import"
    assert real_note is not None
    assert "before origins were recorded" in real_note, "the ordinary row keeps its own sentence"
