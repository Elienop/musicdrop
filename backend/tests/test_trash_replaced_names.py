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
import stat
from pathlib import Path
from typing import Any

import pytest

from app.beets.trash import trash_replaced_files
from app.beets.trash_origins import read_trash_origin
from tests.conftest import protected_for

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
CONTAINER = "Artist - artist art"


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    return folder


def _move(names: list[str], folder: Path, tmp_path: Path) -> Path:
    """The mover, against a Trash root created the way a request creates it.

    ``store_layout.checked_protected_trees`` makes the directory one line before
    it takes the identity, so a mover never sees ``trash=None``; these tests are
    about the NAMES, and ``tests/test_trash_replaced_root.py`` is about the root.
    The origin store is still absent — ``require_usable_store`` is what creates
    that one.
    """
    trash = tmp_path / "trash"
    trash.mkdir(exist_ok=True)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return trash_replaced_files(
            names,
            src_dir_fd=fd,
            container_name=CONTAINER,
            origin=folder,
            trash_dir=trash,
            origins_dir=tmp_path / "trash-origins",
            protected=protected_for(trash_dir=trash, origins_dir=tmp_path / "trash-origins"),
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


def _replace_with_a_newcomer(target: Path, body: bytes) -> tuple[int, int]:
    """Put a NEW entry at ``target``'s name; report ``(target's, newcomer's)`` inode.

    Built at a SIBLING name while ``target`` still holds its inode, then renamed
    over it. Unlinking first and writing at the one name asks the filesystem for
    a fresh inode instead, and one that allocates from a bitmap hands back the
    number just freed: that shape passed here (tmpfs and btrfs reused 0 of 20,
    measured 2026-09-13) and failed on ubuntu-latest, where the newcomer matched
    the guard's ``(st_dev, st_ino)`` and was unlinked. Callers assert the two
    inodes differ, so the premise is read rather than assumed.
    """
    newcomer = target.with_name(f"{target.name}.uploading")
    newcomer.write_bytes(body)
    inodes = (target.lstat().st_ino, newcomer.lstat().st_ino)
    os.replace(newcomer, target)
    return inodes


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
    # Refused before the store check and the allocation: the Trash root is
    # there (the request's own check creates it) and holds nothing, and the
    # origin store was never created, so there is no record to inspect.
    assert list((tmp_path / "trash").iterdir()) == []
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
    assert list((tmp_path / "trash").iterdir()) == []
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
    assert list((tmp_path / "trash").iterdir()) == []


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
    assert list((tmp_path / "trash").iterdir()) == []


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
    swapped: list[tuple[int, int]] = []

    def copy_then_swap(reader: Any, writer: Any, length: int = 0) -> None:
        real_copy(reader, writer)
        # the window between the copy and the unlink through ``src_dir_fd``
        swapped.append(_replace_with_a_newcomer(curated, b"BRAND-NEW-UPLOAD"))

    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(shutil, "copyfileobj", copy_then_swap)

    with caplog.at_level(logging.WARNING, logger="app.beets.trash"):
        dest = _move([curated.name], folder, tmp_path)

    assert len(swapped) == 1, "the swap ran once, inside the copy"
    source_ino, newcomer_ino = swapped[0]  # the source is unchanged since staging
    assert source_ino != newcomer_ino, "the premise: the newcomer is a different inode"
    assert (dest / curated.name).read_bytes() == PNG, "the checked file reached Trash"
    assert curated.read_bytes() == b"BRAND-NEW-UPLOAD", "the newcomer was not unlinked"
    assert "was replaced after it was copied to Trash" in caplog.text
    record = read_trash_origin(tmp_path / "trash-origins", dest.name)
    assert record is not None, "what did move is still recorded"


@pytest.mark.parametrize("field", ["size", "mtime"])
def test_a_source_rewritten_in_place_during_the_copy_is_left_in_place(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rewrite IN PLACE keeps the inode, so size and mtime are what see it.

    The copy in Trash holds the bytes that were checked; the source now holds
    different ones. Leaving it is the end state that loses neither. One case per
    field, because either one alone would let the other's clause be dropped.
    """
    folder = _folder(tmp_path)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG)
    staged = curated.lstat()
    real_copy = shutil.copyfileobj

    def copy_then_rewrite(reader: Any, writer: Any, length: int = 0) -> None:
        real_copy(reader, writer)
        # ``utime`` rather than the clock: a filesystem with a coarse mtime could
        # otherwise give the rewrite the staged value and move both fields at once.
        if field == "size":
            curated.write_bytes(PNG + b"MORE")
            os.utime(curated, ns=(staged.st_atime_ns, staged.st_mtime_ns))
        else:
            curated.write_bytes(bytes(len(PNG)))
            os.utime(curated, ns=(staged.st_atime_ns, staged.st_mtime_ns + 10**9))

    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(shutil, "copyfileobj", copy_then_rewrite)

    with caplog.at_level(logging.WARNING, logger="app.beets.trash"):
        dest = _move([curated.name], folder, tmp_path)

    left = curated.lstat()
    assert left.st_ino == staged.st_ino, "the premise: a rewrite in place keeps the inode"
    moved_size = left.st_size != staged.st_size
    moved_mtime = left.st_mtime_ns != staged.st_mtime_ns
    assert (moved_size, moved_mtime) == (field == "size", field == "mtime"), (
        f"the premise: {field} alone moved, got size={moved_size} mtime={moved_mtime}"
    )
    assert curated.read_bytes() != PNG, "the rewritten source was not unlinked"
    assert (dest / curated.name).read_bytes() == PNG, "Trash holds the checked bytes"
    assert "was replaced after it was copied to Trash" in caplog.text


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
    swapped: list[tuple[int, int]] = []

    def recreate_then_swap(src: Any, dst: Any, **kwargs: Any) -> None:
        real_symlink(src, dst, **kwargs)
        if "dir_fd" in kwargs:  # the copy into the container, not the app's others
            swapped.append(_replace_with_a_newcomer(linked, b"BRAND-NEW-UPLOAD"))

    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(os, "symlink", recreate_then_swap)

    dest = _move([linked.name], folder, tmp_path)

    assert len(swapped) == 1, "the swap ran once, inside the copy's symlink call"
    symlink_ino, newcomer_ino = swapped[0]  # the symlink is unchanged since staging
    assert symlink_ino != newcomer_ino, "the premise: the newcomer is a different inode"
    assert (dest / linked.name).is_symlink()
    assert os.readlink(dest / linked.name) == "../elsewhere/wall.png"
    assert not linked.is_symlink()
    assert linked.read_bytes() == b"BRAND-NEW-UPLOAD", "the newcomer was not unlinked"


def test_a_newcomer_handed_the_freed_inode_is_refused_by_its_TYPE(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The inode reuse itself, SIMULATED — the filesystems reachable here do not.

    What the runner did (2026-09-13): a regular file written at a symlink's
    just-freed number matched the guard's ``(st_dev, st_ino)``. tmpfs and btrfs
    reused 0 of 20 (measured 2026-09-13), so the collision is forged instead —
    the guard's own re-``lstat`` is wrapped and only its ``st_dev``/``st_ino``
    overwritten, which is the one thing a real reuse changes. Size and mtime are
    matched ON DISK, so ``S_IFMT`` is the only field left to refuse it.
    """
    folder = _folder(tmp_path)
    linked = folder / "artist-background.png"
    linked.symlink_to("../elsewhere/wall.png")
    staged = linked.lstat()  # the identity the mover stages, unchanged until the swap
    # A symlink's size is its target's length, so the newcomer is padded to it.
    newcomer_bytes = b"NEWCOMER".ljust(staged.st_size, b"-")
    real_symlink, real_stat = os.symlink, os.stat
    forged: list[os.stat_result] = []

    def recreate_then_swap(src: Any, dst: Any, **kwargs: Any) -> None:
        real_symlink(src, dst, **kwargs)
        if "dir_fd" not in kwargs:  # the app's other symlinks, not the copy
            return
        sibling = folder / f"{linked.name}.uploading"
        sibling.write_bytes(newcomer_bytes)
        os.utime(sibling, ns=(staged.st_atime_ns, staged.st_mtime_ns))
        os.replace(sibling, linked)

    def stat_as_if_the_number_was_reused(path: Any, **kwargs: Any) -> os.stat_result:
        """The SIMULATION: the newcomer reported under the symlink's identity."""
        got = real_stat(path, **kwargs)
        if not (
            path == linked.name  # the mover's bare name, not a Path from elsewhere
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
            and stat.S_ISREG(got.st_mode)  # false at staging time: still a symlink
        ):
            return got
        as_reused = os.stat_result(
            (
                got.st_mode,
                staged.st_ino,
                staged.st_dev,
                got.st_nlink,
                got.st_uid,
                got.st_gid,
                got.st_size,
                int(got.st_atime),
                int(got.st_mtime),
                int(got.st_ctime),
            ),
            {  # the 10-tuple form rounds to seconds; the guard reads the ns field
                "st_atime_ns": got.st_atime_ns,
                "st_mtime_ns": got.st_mtime_ns,
                "st_ctime_ns": got.st_ctime_ns,
            },
        )
        forged.append(as_reused)
        return as_reused

    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(os, "symlink", recreate_then_swap)
    monkeypatch.setattr(os, "stat", stat_as_if_the_number_was_reused)

    with caplog.at_level(logging.WARNING, logger="app.beets.trash"):
        dest = _move([linked.name], folder, tmp_path)

    monkeypatch.undo()
    assert len(forged) == 1, "the guard's re-lstat is the call that got the forgery"
    current = forged[0]
    assert (current.st_dev, current.st_ino) == (staged.st_dev, staged.st_ino), "reuse simulated"
    assert current.st_size == staged.st_size, "size is not what refuses this"
    assert current.st_mtime_ns == staged.st_mtime_ns, "mtime is not what refuses this"
    assert stat.S_IFMT(current.st_mode) != stat.S_IFMT(staged.st_mode), "the type is"
    assert linked.read_bytes() == newcomer_bytes, "the newcomer was not unlinked"
    assert (dest / linked.name).is_symlink(), "what was checked still reached Trash"
    assert "was replaced after it was copied to Trash" in caplog.text


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


def test_a_directory_whose_identity_was_staged_is_not_relocated(tmp_path: Path) -> None:
    """The rename arm checks identity AND type, because identity is not unique
    over time.

    ``(st_dev, st_ino)`` from the staging lstat is what the post-rename lstat is
    compared against, and ext4/xfs allocate inodes from a bitmap — a freed
    regular file's number is handed out again. Staging a DIRECTORY's own
    identity is exactly what such a filesystem gives the compare, and measured
    with the type clause removed, the whole tree was relocated into the
    container: the outcome the guard exists to stop.
    """
    from app.beets.trash import _move_between_fds

    src = tmp_path / "music" / "Artist"
    src.mkdir(parents=True)
    dst = tmp_path / "container"
    dst.mkdir()
    tree = src / "artist-poster.png"
    tree.mkdir()
    (tree / "inside.flac").write_bytes(b"a tree")

    src_fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY)
    dst_fd = os.open(dst, os.O_RDONLY | os.O_DIRECTORY)
    try:
        st = os.stat(tree.name, dir_fd=src_fd, follow_symlinks=False)
        with pytest.raises(OSError) as caught:
            _move_between_fds(tree.name, src_dir_fd=src_fd, dst_dir_fd=dst_fd, st=st)
    finally:
        os.close(dst_fd)
        os.close(src_fd)

    assert caught.value.strerror == "the file changed after it was checked"
    assert (tree / "inside.flac").read_bytes() == b"a tree", "put back, whole"
    assert list(dst.iterdir()) == [], "and nothing stayed in the container"


def _fsync_that_refuses_directories(err: int) -> Any:
    """``os.fsync`` answering ``err`` for a DIRECTORY fd, real for anything else.

    The copy's own fsync is a correctness precondition — the bytes must be on
    disk before the source is unlinked — and must keep raising; only the
    container's DIRECTORY fsync is the durability extra this swallows.
    """
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(err, os.strerror(err))
        real_fsync(fd)

    return fsync


def test_a_container_that_cannot_be_fsynced_still_completes_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOTSUP from the container fsync is not a failed move.

    The copy arm exists BECAUSE the Trash is on another filesystem, and a FUSE
    or network mount can answer "not supported" for an fsync on a directory —
    measured (security seat L-6): the copy was in Trash, the source still on
    disk, and the caller reported the art write refused.
    """
    folder = _folder(tmp_path)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG)
    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(os, "fsync", _fsync_that_refuses_directories(errno.ENOTSUP))

    dest = _move([curated.name], folder, tmp_path)

    assert (dest / curated.name).read_bytes() == PNG
    assert not curated.exists(), "the source was still unlinked last"


def test_a_real_fsync_failure_on_the_container_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only "this filesystem cannot" is swallowed: EIO is a fault, so the source
    stays where it is and the caller hears about it."""
    folder = _folder(tmp_path)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG)
    monkeypatch.setattr(os, "rename", _across_a_device_boundary(os.rename))
    monkeypatch.setattr(os, "fsync", _fsync_that_refuses_directories(errno.EIO))

    with pytest.raises(OSError) as caught:
        _move([curated.name], folder, tmp_path)

    assert caught.value.errno == errno.EIO
    assert curated.read_bytes() == PNG, "not unlinked"
