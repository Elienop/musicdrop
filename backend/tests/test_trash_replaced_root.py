"""The Trash ROOT the move-aside writes into is the one the layout check examined.

``store_layout`` deliberately PERMITS a Trash strictly inside the music library
(``store_layout.py``'s ``T`` row — the owner's ruling, because deletes are then
same-disk renames). In that layout the attacker of the threat model
(``BACKLOG.md``: a party who can write inside the music library) owns the Trash
path's PARENT, so it can replace the Trash directory itself after the
per-request check. Measured before this: the container and the file landed in a
directory of the attacker's choosing, the checked Trash stayed empty, and the
origin record named an entry that does not exist.

The identity is the one ``protected_trees`` stat'd beside ``checked_store_dirs``,
and ``protected.open_checked_dir`` is what re-asks the kernel for it — the same
descriptor ``trash_manage.empty_all`` enumerates through.
"""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path

import pytest
from beets.library import Library

from app.beets.artist_art import ArtTrashStore, get_artist_dirs, write_artist_art
from app.beets.protected import ProtectedTrees
from app.beets.trash import trash_replaced_files
from app.beets.trash_origins import read_trash_origin
from tests.conftest import protected_for

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
CONTAINER = "Artist - artist art"


def _record(trash_dir: Path, tmp_path: Path) -> ProtectedTrees:
    """The identities a destructive request builds, taken NOW.

    The suite's own ``protected_for``, which is the real
    ``app.beets.protected.protected_trees``, so what these tests hand the mover
    is what a request hands it. A Trash dir that is not there when this runs has
    no identity and comes back as ``trash=None``, which the mover refuses: the
    creation happens one line earlier in production
    (``store_layout._ensure_trash_root``), never here.
    """
    return protected_for(trash_dir=trash_dir, origins_dir=tmp_path / "trash-origins")


def _library(tmp_path: Path) -> tuple[Path, Path]:
    """``(the artist folder, the Trash inside the music library)`` — both real."""
    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    (folder / "artist-poster.png").write_bytes(PNG)
    trash = tmp_path / "music" / ".trash"
    trash.mkdir()
    return folder, trash


def _move(folder: Path, trash_dir: Path, protected: ProtectedTrees, tmp_path: Path) -> Path:
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return trash_replaced_files(
            ["artist-poster.png"],
            src_dir_fd=fd,
            container_name=CONTAINER,
            origin=folder,
            trash_dir=trash_dir,
            origins_dir=tmp_path / "trash-origins",
            protected=protected,
        )
    finally:
        os.close(fd)


def test_a_trash_root_swapped_for_a_symlink_after_the_check_is_refused(tmp_path: Path) -> None:
    """The measured escape: the file left the library for a directory the
    attacker named, while the user was told to look in Trash."""
    folder, trash = _library(tmp_path)
    protected = _record(trash, tmp_path)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.rename(trash, tmp_path / "real-trash")
    os.symlink(elsewhere, trash)

    with pytest.raises(OSError) as caught:
        _move(folder, trash, protected, tmp_path)

    assert caught.value.errno == errno.EINVAL
    assert caught.value.strerror == "the Trash directory changed after it was checked"
    assert (folder / "artist-poster.png").read_bytes() == PNG
    assert list(elsewhere.iterdir()) == [], "nothing landed outside the checked Trash"
    assert list((tmp_path / "real-trash").iterdir()) == []
    assert read_trash_origin(tmp_path / "trash-origins", CONTAINER) is None


def test_a_different_directory_at_the_trash_path_is_refused(tmp_path: Path) -> None:
    """Not only a symlink: a REAL directory renamed onto the path is a different
    inode, and the compare is what sees that. ``O_NOFOLLOW`` alone would open it.
    """
    folder, trash = _library(tmp_path)
    protected = _record(trash, tmp_path)
    os.rename(trash, tmp_path / "real-trash")
    trash.mkdir()

    with pytest.raises(OSError) as caught:
        _move(folder, trash, protected, tmp_path)

    assert caught.value.strerror == "the Trash directory changed after it was checked"
    assert (folder / "artist-poster.png").read_bytes() == PNG
    assert list(trash.iterdir()) == []
    assert read_trash_origin(tmp_path / "trash-origins", CONTAINER) is None


def test_a_trash_root_that_vanished_after_the_check_is_refused_not_recreated(
    tmp_path: Path,
) -> None:
    """A Trash that was there at check time and is gone now is not re-created:
    a fresh directory has no relationship to the identity that was examined, and
    creating one to then refuse it would leave litter in its parent."""
    folder, trash = _library(tmp_path)
    protected = _record(trash, tmp_path)
    trash.rmdir()

    with pytest.raises(OSError) as caught:
        _move(folder, trash, protected, tmp_path)

    assert caught.value.strerror == "the Trash directory changed after it was checked"
    assert not trash.exists(), "refused without re-creating it"
    assert (folder / "artist-poster.png").read_bytes() == PNG


def test_a_trash_root_with_no_identity_is_refused_by_the_mover(tmp_path: Path) -> None:
    """The mover NEVER creates the root, so ``trash=None`` is a refusal.

    The Trash is created where its identity is taken
    (``store_layout._ensure_trash_root``), so ``None`` means the directory went
    away between those two lines — measured (probe_2a2), a party who owns the
    Trash's parent re-opens that window at will by renaming the real Trash
    aside, and the arm that created the root here then accepted whatever they
    left at the path. A refusal is the safe end of that: nothing moved.
    """
    folder, trash = _library(tmp_path)
    away = tmp_path / "carried-off"
    os.rename(trash, away)
    protected = _record(trash, tmp_path)
    assert protected.trash is None, "the premise: nothing was there to stat"
    trash.mkdir()  # whatever is at the path now, checked or not

    with pytest.raises(OSError) as caught:
        _move(folder, trash, protected, tmp_path)

    assert caught.value.errno == errno.EINVAL
    assert caught.value.strerror == "the Trash directory could not be examined when it was checked"
    assert (folder / "artist-poster.png").read_bytes() == PNG
    assert list(trash.iterdir()) == []
    assert read_trash_origin(tmp_path / "trash-origins", CONTAINER) is None


def test_a_symlink_at_an_unchecked_trash_root_is_refused_before_it_is_opened(
    tmp_path: Path,
) -> None:
    """A symlink at a Trash path that was never checked: refused on the identity.

    It used to reach an ``O_NOFOLLOW`` open, which refused a link at the LEAF and
    followed one at any component above it (security seat M-3). Nothing opens
    this path now until an identity exists for it.
    """
    folder, real = _library(tmp_path)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.rename(real, tmp_path / "real-trash")
    unchecked = _record(tmp_path / "not-created-yet", tmp_path)
    assert unchecked.trash is None
    os.symlink(elsewhere, real)

    with pytest.raises(OSError) as caught:
        _move(folder, real, unchecked, tmp_path)

    assert caught.value.strerror == "the Trash directory could not be examined when it was checked"
    assert list(elsewhere.iterdir()) == []
    assert (folder / "artist-poster.png").read_bytes() == PNG


def test_the_refusal_keeps_the_trash_path_out_of_its_message(tmp_path: Path) -> None:
    """The refusal is an ``OSError``, not a ``ProtectedTreeError``.

    The reset endpoint relays ``strerror`` and keeps the configured path off the
    wire (``tests/test_artist_image_reset_to_trash.py::
    test_the_503_carries_the_oserrors_strerror_and_no_server_path``), while
    ``ProtectedTreeError``'s own message spells the path in full. The path is
    still carried, as ``filename``, for the log line.
    """
    folder, trash = _library(tmp_path)
    protected = _record(trash, tmp_path)
    os.rename(trash, tmp_path / "real-trash")
    trash.mkdir()

    with pytest.raises(OSError) as caught:
        _move(folder, trash, protected, tmp_path)

    assert str(trash) not in str(caught.value.strerror)
    assert caught.value.filename == str(trash)


def test_the_art_writer_reports_failed_and_writes_nothing_when_the_root_was_swapped(
    edit_lib: Library, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The forced write's half of the same refusal, end to end.

    ``_move_aside`` turns the mover's ``OSError`` into ``ArtTrashRefusedError``
    with one WARNING naming the folder and how many files would have been
    replaced, and ``_write_folder``'s order means nothing is written into a
    folder whose old art did not move. Without that arm the outcome is the same
    ``failed`` and the log line is the only difference — so the line is what
    this asserts.

    ONE line per refused folder: ``write_artist_art`` used to log a second
    record, with a second traceback, for the refusal ``_move_aside`` had just
    reported (code seat S10).
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    origins = tmp_path / "trash-origins"
    store = ArtTrashStore(
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(trash_dir=trash, origins_dir=origins),
    )
    name = str(next(iter(edit_lib.albums())).albumartist)
    dirs = get_artist_dirs(edit_lib, name)
    assert dirs
    for folder in dirs:  # the curated files, seeded by hand
        (folder / "artist-poster.png").write_bytes(PNG)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.rename(trash, tmp_path / "real-trash")
    os.symlink(elsewhere, trash)

    with caplog.at_level(logging.WARNING, logger="app.beets.artist_art"):
        out = write_artist_art(
            edit_lib,
            name,
            poster=(PNG, "image/png"),
            background=None,
            force=True,
            trash=store,
        )

    assert (out.status, out.written) == ("failed", 0)
    assert "file(s) it would replace could not be moved to Trash" in caplog.text
    lines = [r for r in caplog.records if r.name == "app.beets.artist_art"]
    assert len(lines) == len(dirs), "one record per refused folder, not two"
    assert lines[0].exc_info is not None, "and it carries the traceback"
    for folder in dirs:
        assert (folder / "artist-poster.png").read_bytes() == PNG, "the curated file stayed"
    assert list(elsewhere.iterdir()) == []
    assert list((tmp_path / "real-trash").iterdir()) == []


def test_a_trash_aliased_onto_another_store_says_so_rather_than_racing(
    tmp_path: Path,
) -> None:
    """The bind-mount alias arm keeps its own cause (security seat L-5).

    ``open_checked_dir`` refuses three different things and the mover used to
    relay one sentence for all of them, so an operator whose Trash IS the origin
    store — what a bind mount does, and what every spelled layout row allows —
    was told the directory had changed under the request.
    """
    folder, trash = _library(tmp_path)
    aliased = protected_for(trash_dir=trash, origins_dir=trash)
    assert aliased.trash_alias is not None, "the premise: one inode, two stores"

    with pytest.raises(OSError) as caught:
        _move(folder, trash, aliased, tmp_path)

    assert caught.value.errno == errno.EINVAL
    assert (
        caught.value.strerror
        == "the Trash directory is the same folder as another MusicDrop directory"
    )
    assert caught.value.filename == str(trash), "the path travels off the wire"
    assert (folder / "artist-poster.png").read_bytes() == PNG
    assert list(trash.iterdir()) == []
