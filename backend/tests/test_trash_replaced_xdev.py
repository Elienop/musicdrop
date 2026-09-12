"""The art move-aside across a REAL filesystem boundary, not a spied one.

Every other test of the EXDEV arm forces the errno: ``tests/test_artist_art_write.py``
wraps ``os.rename`` to raise it for the ``dst_dir_fd`` spelling, and
``tests/test_trash_origin_record.py:1739``
(``test_a_part_way_cross_filesystem_move_says_the_folder_may_be_in_both``) and
``tests/test_trash_restore_failure_arms.py``, whose module docstring (``:21``)
says why that branch needs its own coverage, do the same for the folder movers.
A spy proves the code the arm runs; it cannot prove the arm is the one the
kernel picks. This file complements them by putting Trash on ``/dev/shm`` and
the library on ``tmp_path`` — two superblocks — so ``os.rename`` between the two
descriptors fails on its own, and asserting that it did is the point of the
file. That layout is the shipped one: in Docker Trash is ``<beets_dir>/trash``
on ``/data`` and the library is ``/music``.

``MUSICDROP_REQUIRE_XDEV=1`` (set on CI) turns "no second filesystem here" from
a skip into a failure, so the one test that measures this cannot go quietly
green everywhere.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.beets.trash import trash_replaced_files
from app.beets.trash_origins import read_trash_origin
from tests.conftest import protected_for

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
SIBLING = b"the file the planted symlink points at"
#: Distinct atime and mtime, so a copy that carried the pair the wrong way round
#: fails the mtime assertion below. The ATIME itself is deliberately NOT asserted:
#: reading the copy back bumps it under ``relatime`` (measured — 2026-09-12, the
#: first ``read_bytes`` of the moved file), so it cannot be pinned after the read.
ATIME_NS = 1_500_000_000_000_000_000
MTIME_NS = 1_400_000_000_000_000_000

SHM = Path("/dev/shm")


def _why_not(tmp_path: Path) -> str | None:
    """Why this box cannot run a real cross-device move, or ``None``."""
    if not SHM.is_dir() or not os.access(SHM, os.W_OK | os.X_OK):
        return f"{SHM} is not a writable directory on this box"
    shm_dev, lib_dev = os.stat(SHM).st_dev, os.stat(tmp_path).st_dev
    if shm_dev == lib_dev:
        return (
            f"{SHM} (st_dev {shm_dev}) and {tmp_path} (st_dev {lib_dev}) are on ONE "
            "filesystem, so os.rename between them cannot raise EXDEV"
        )
    return None


@pytest.fixture
def shm_trash(tmp_path: Path) -> Iterator[Path]:
    """A Trash dir on another filesystem than the library ``tmp_path`` holds.

    Skipped when this box has no second writable filesystem — unless
    ``MUSICDROP_REQUIRE_XDEV=1``, where the same condition FAILS instead: a
    silent skip is how the only real EXDEV coverage in the suite would stop
    running without anyone noticing.
    """
    reason = _why_not(tmp_path)
    if reason is not None:
        if os.environ.get("MUSICDROP_REQUIRE_XDEV") == "1":
            pytest.fail(f"MUSICDROP_REQUIRE_XDEV=1 and {reason}")
        pytest.skip(reason)
    root = Path(tempfile.mkdtemp(dir=SHM, prefix="musicdrop-xdev-"))
    trash = root / "trash"
    # Created here, the way a request's own check creates it before taking the
    # identity the mover compares (``store_layout._ensure_trash_root``).
    trash.mkdir()
    try:
        yield trash
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_real_cross_device_move_aside_carries_mode_mtime_and_the_symlink(
    tmp_path: Path, shm_trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trash on another filesystem: the copy fallback is what the kernel forces.

    ``os.rename`` is wrapped to RECORD, not to raise — the EXDEV in ``crossed``
    comes from the kernel, once per entry. What the container then holds is
    asserted against the source it replaced: the bytes, the mode and
    ``st_mtime_ns`` of the curated poster, and the planted symlink recreated as
    a symlink with its own target rather than followed and copied through. The
    sibling it points at is not in ``names`` and must be untouched.

    ``umask 0o077`` trims the copy's create mode to 0o600, so 0o640 below can
    only come from the ``fchmod`` on the copy's own fd.
    """
    folder = tmp_path / "music" / "Artist"
    folder.mkdir(parents=True)
    curated = folder / "artist-poster.png"
    curated.write_bytes(PNG)
    curated.chmod(0o640)
    os.utime(curated, ns=(ATIME_NS, MTIME_NS))
    sibling = folder / "wall.png"
    sibling.write_bytes(SIBLING)
    linked = folder / "artist-background.png"
    linked.symlink_to(sibling.name)
    origins = tmp_path / "trash-origins"

    real_rename = os.rename
    crossed: list[int | None] = []

    def recording_rename(*args: Any, **kwargs: Any) -> None:
        try:
            real_rename(*args, **kwargs)
        except OSError as exc:
            crossed.append(exc.errno)
            raise

    monkeypatch.setattr(os, "rename", recording_rename)

    old_umask = os.umask(0o077)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        dest = trash_replaced_files(
            [curated.name, linked.name],
            src_dir_fd=fd,
            container_name="Artist - artist art",
            origin=folder,
            trash_dir=shm_trash,
            origins_dir=origins,
            protected=protected_for(trash_dir=shm_trash, origins_dir=origins),
        )
    finally:
        os.close(fd)
        os.umask(old_umask)

    assert crossed == [errno.EXDEV, errno.EXDEV], (
        "the kernel refused both renames itself; nothing here simulates the errno"
    )
    assert dest.parent == shm_trash
    assert dest.stat().st_dev != folder.stat().st_dev, "the container really is elsewhere"

    moved = dest / curated.name
    assert not moved.is_symlink()
    assert moved.read_bytes() == PNG
    assert moved.stat().st_mode & 0o777 == 0o640
    assert moved.stat().st_mtime_ns == MTIME_NS

    copied_link = dest / linked.name
    assert copied_link.is_symlink(), "a symlink is recreated, never followed and copied"
    assert os.readlink(copied_link) == sibling.name
    assert sibling.read_bytes() == SIBLING, "the sibling the link named was not moved"

    # The sources are unlinked LAST and only the two named ones went.
    assert sorted(p.name for p in folder.iterdir()) == [sibling.name]

    record = read_trash_origin(origins, dest.name)
    assert record is not None
    assert record.moved == "files", "a hand copy back, not a folder move-back"
    assert record.origin == str(folder)
