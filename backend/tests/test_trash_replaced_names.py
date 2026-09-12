"""What ``trash_replaced_files`` accepts as a name, and what it re-checks.

Three guards, all of them about the gap between "the mover lstat'd this" and
"the kernel acted on that":

* a name that is not a bare directory entry is refused before anything moves —
  an absolute one used to be renamed onto itself and REPORTED success, and
  ``../x`` used to climb out of both descriptors;
* the rename arm compares the landed file's IDENTITY, not only its type;
* the cross-device copy re-lstat's the source immediately before the unlink, so
  a file that replaced it after the copy is left where it is.

``tests/test_artist_art_write.py`` holds the mover's happy paths and its
container-claim guards; ``tests/test_trash_replaced_xdev.py`` runs the copy arm
across two real filesystems. The EXDEV here is forced, because these tests are
about the window inside the arm rather than about reaching it.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.beets.trash import trash_replaced_files
from app.beets.trash_origins import read_trash_origin

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
CONTAINER = "Artist - artist art"


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    return folder


def _move(names: list[str], folder: Path, tmp_path: Path) -> Path:
    """The mover, against a Trash + origin store that do not exist yet."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return trash_replaced_files(
            names,
            src_dir_fd=fd,
            container_name=CONTAINER,
            origin=folder,
            trash_dir=tmp_path / "trash",
            origins_dir=tmp_path / "trash-origins",
        )
    finally:
        os.close(fd)


def _across_a_device_boundary(real_rename: Any) -> Any:
    """``os.rename`` that answers EXDEV for the mover's own two-descriptor form."""

    def rename(*args: Any, **kwargs: Any) -> None:
        if "dst_dir_fd" in kwargs:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(*args, **kwargs)

    return rename


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "sub/f", "/etc/passwd", f"a{os.sep}b"],
    ids=["empty", "dot", "dotdot", "separator", "absolute", "os-sep"],
)
def test_a_name_that_is_not_a_bare_entry_is_refused(name: str, tmp_path: Path) -> None:
    """The contract the whole anchoring rests on, enforced rather than documented.

    ``os.sep`` and ``/`` both, because the errno is not what tells these apart:
    ``""`` answered ENOENT and ``"."`` answered "not a regular file or a
    symlink" — EINVAL, the same errno this raises — so the STRERROR is what a
    caller (and this test) reads.
    """
    folder = _folder(tmp_path)
    (folder / "artist-poster.png").write_bytes(PNG)

    with pytest.raises(OSError) as caught:
        _move([name], folder, tmp_path)

    assert caught.value.errno == errno.EINVAL
    assert caught.value.strerror == "not a bare entry name"
    assert caught.value.filename == name
    # Refused before the store check, the mkdir and the allocation: neither
    # directory exists, so there is no container and no record to inspect.
    assert not (tmp_path / "trash").exists()
    assert not (tmp_path / "trash-origins").exists()


def test_an_absolute_name_cannot_report_a_move_it_did_not_make(tmp_path: Path) -> None:
    """The measured harm: ``rename(abs, abs)`` succeeded, ``moved`` counted it,
    the origin record was written, and the caller's move-aside gate then wrote
    over the curated file with nothing in Trash.
    """
    folder = _folder(tmp_path)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG)

    with pytest.raises(OSError) as caught:
        _move([str(curated)], folder, tmp_path)

    assert caught.value.strerror == "not a bare entry name"
    assert curated.read_bytes() == PNG
    assert not (tmp_path / "trash").exists()
    assert read_trash_origin(tmp_path / "trash-origins", CONTAINER) is None


def test_a_climbing_name_cannot_take_a_file_out_of_the_library(tmp_path: Path) -> None:
    """``../library.db`` un-anchored BOTH ends: measured, the database left the
    music library and landed loose at the Trash ROOT, where neither half of
    ``trash_manage.list_trashed_albums`` lists it.
    """
    folder = _folder(tmp_path)
    database = folder.parent / "library.db"
    database.write_bytes(b"THE-DATABASE")

    with pytest.raises(OSError) as caught:
        _move(["../library.db"], folder, tmp_path)

    assert caught.value.strerror == "not a bare entry name"
    assert database.read_bytes() == b"THE-DATABASE"
    assert not (tmp_path / "trash").exists()


def test_a_separator_cannot_reach_a_file_outside_the_anchored_folder(tmp_path: Path) -> None:
    """A ``dir_fd`` anchors only the FIRST component, so ``sub/f`` is resolved
    through a symlinked ``sub`` even with ``follow_symlinks=False`` — the stat
    that stages the move used to reach a file outside the descriptor.
    """
    folder = _folder(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_bytes(b"OUTSIDE-THE-LIBRARY")
    (folder / "sub").symlink_to(outside)

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        reached = os.stat("sub/f", dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    assert reached.st_ino == (outside / "f").stat().st_ino, "the premise: it IS reachable"

    with pytest.raises(OSError) as caught:
        _move(["sub/f"], folder, tmp_path)

    assert caught.value.strerror == "not a bare entry name"
    assert (outside / "f").read_bytes() == b"OUTSIDE-THE-LIBRARY"
    assert not (tmp_path / "trash").exists()


def test_a_source_swapped_before_the_rename_is_put_back_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename arm's post-move lstat is an IDENTITY check, not a type check.

    Measured before it: a different regular file renamed onto the name between
    the staging lstat and the move was accepted (``checked 2264295, landed
    2264296``), so the Trash entry and its record named a file that is not the
    one in it. The copy arm already compared identity; the two agree now.
    """
    folder = _folder(tmp_path)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG)
    stranger = folder / "stranger.png"
    stranger.write_bytes(b"A-DIFFERENT-FILE")
    real_rename = os.rename
    swapped: list[bool] = []

    def swap_then_rename(*args: Any, **kwargs: Any) -> None:
        if "dst_dir_fd" in kwargs and not swapped:
            swapped.append(True)
            real_rename(stranger, curated)  # another inode, on the guarded name
        real_rename(*args, **kwargs)

    monkeypatch.setattr(os, "rename", swap_then_rename)

    with pytest.raises(OSError) as caught:
        _move([curated.name], folder, tmp_path)

    assert swapped, "the swap never ran"
    assert caught.value.strerror == "the file changed after it was checked"
    assert curated.read_bytes() == b"A-DIFFERENT-FILE", "renamed back where it was"
    assert list((tmp_path / "trash").iterdir()) == []  # the container went with it
    assert read_trash_origin(tmp_path / "trash-origins", CONTAINER) is None


def test_a_file_that_replaced_the_source_after_the_copy_is_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """There is no unlink-by-fd, so the copy arm re-lstat's the name first.

    Measured without it: a file that took the source's name after the copy was
    unlinked without ever reaching Trash. The conservative end state is both —
    the copy in Trash and the new file on disk — and one log line, because they
    then read as a duplicate.
    """
    folder = _folder(tmp_path)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG)
    real_copy = shutil.copyfileobj

    def copy_then_swap(reader: Any, writer: Any, length: int = 0) -> None:
        real_copy(reader, writer)
        # the window between the copy and the unlink through ``src_dir_fd``
        curated.unlink()
        curated.write_bytes(b"BRAND-NEW-UPLOAD")

    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(shutil, "copyfileobj", copy_then_swap)

    with caplog.at_level(logging.WARNING, logger="app.beets.trash"):
        dest = _move([curated.name], folder, tmp_path)

    assert (dest / curated.name).read_bytes() == PNG, "the checked file reached Trash"
    assert curated.read_bytes() == b"BRAND-NEW-UPLOAD", "the newcomer was not unlinked"
    assert "was replaced after it was copied to Trash" in caplog.text
    record = read_trash_origin(tmp_path / "trash-origins", dest.name)
    assert record is not None, "what did move is still recorded"


def test_a_symlink_that_was_replaced_after_it_was_recreated_is_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy arm's symlink sub-branch has the same window and the same guard.

    ``readlink`` then ``symlink`` then ``unlink`` is three resolutions of one
    name; the re-lstat is what keeps the third from hitting a regular file that
    arrived in between.
    """
    folder = _folder(tmp_path)
    linked = folder / "artist-background.png"
    linked.symlink_to("../elsewhere/wall.png")
    real_symlink = os.symlink

    def recreate_then_swap(src: Any, dst: Any, **kwargs: Any) -> None:
        real_symlink(src, dst, **kwargs)
        if "dir_fd" in kwargs:  # the copy into the container, not the app's others
            linked.unlink()
            linked.write_bytes(b"BRAND-NEW-UPLOAD")

    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(os, "symlink", recreate_then_swap)

    dest = _move([linked.name], folder, tmp_path)

    assert (dest / linked.name).is_symlink()
    assert os.readlink(dest / linked.name) == "../elsewhere/wall.png"
    assert not linked.is_symlink()
    assert linked.read_bytes() == b"BRAND-NEW-UPLOAD", "the newcomer was not unlinked"


def test_the_container_is_fsynced_before_the_copied_source_is_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the copy and the unlink must not lose both.

    The copy's own bytes are fsynced through its fd; the container's new ENTRY
    needs the DIRECTORY's, and only then may the one remaining name go. Recorded
    by inode because the file's fsync and the directory's are the same call.
    """
    folder = _folder(tmp_path)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG)
    real_fsync, real_unlink = os.fsync, os.unlink
    events: list[tuple[str, int | str]] = []

    def recording_fsync(fd: int) -> None:
        events.append(("fsync", os.fstat(fd).st_ino))
        real_fsync(fd)

    def recording_unlink(path: Any, *, dir_fd: int | None = None) -> None:
        events.append(("unlink", str(path)))
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "unlink", recording_unlink)

    dest = _move([curated.name], folder, tmp_path)

    monkeypatch.undo()
    unlinked = events.index(("unlink", curated.name))
    assert ("fsync", dest.stat().st_ino) in events[:unlinked], (
        f"the container was not fsynced before the source went: {events}"
    )
