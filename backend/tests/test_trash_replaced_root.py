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
import os
from pathlib import Path

import pytest

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
    is what a request hands it. A Trash dir that does not exist yet has no
    identity and comes back as ``trash=None`` — the first-use arm, and the same
    absence production starts from.
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


def test_the_first_use_creates_the_trash_root_and_moves(tmp_path: Path) -> None:
    """``protected.trash`` is None until something creates the Trash, and this
    mover is one of the four things that do.

    Nothing creates it at startup, so refusing here would fail the first forced
    art write (and the first artist-image reset) of a fresh install. There is no
    identity to compare against a directory that did not exist.
    """
    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    (folder / "artist-poster.png").write_bytes(PNG)
    trash = tmp_path / "beets" / "trash"
    protected = _record(trash, tmp_path)
    assert protected.trash is None, "the premise: nothing was there to stat"

    dest = _move(folder, trash, protected, tmp_path)

    assert dest.parent == trash
    assert (dest / "artist-poster.png").read_bytes() == PNG
    assert not (folder / "artist-poster.png").exists()


def test_a_symlink_at_an_unchecked_trash_root_is_still_refused(tmp_path: Path) -> None:
    """What the first-use arm keeps: the open is ``O_NOFOLLOW``.

    A symlink planted at the Trash path before anything created it used to be
    followed. What it does NOT cover is a real directory a stranger left there,
    which needs write on the Trash's parent — for a Trash inside the music
    library that is the layout the owner allows, and the default
    ``<beets_dir>/trash`` is out of reach because ``store_layout`` refuses a
    beets dir inside the library.
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

    assert caught.value.errno in (errno.ELOOP, errno.ENOTDIR), caught.value
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
