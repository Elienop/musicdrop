"""A hardlink across filesystems fails the job with one plain line, then beets' (design S5 #7).

Driven through beets' own ``util.hardlink``, with only the ``link(2)`` call
underneath it stubbed to fail: the recognition reads the exception beets builds
and the ``OSError`` it builds it inside, so a beets bump that changes either
shape fails here (decisions #52) instead of letting the plain line go quiet.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest
from beets import util
from beets.autotag.match import Recommendation as BeetsRec
from beets.util import FilesystemError

from app.beets.import_session import (
    CROSS_DEVICE_HARDLINK,
    ImportBridge,
    is_cross_device_hardlink,
)
from tests.test_import_incremental_e2e import (
    _import,
    _install_lookup,
    _library,
    _source_folder,
)


def _link_fails_with(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """``os.link``, the call under beets' ``Path.hardlink_to``, fails with ``code``."""

    def refuse(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        raise OSError(code, os.strerror(code), os.fsdecode(src), None, os.fsdecode(dst))

    monkeypatch.setattr(os, "link", refuse)


def _beets_hardlink_error(tmp_path: Path) -> FilesystemError:
    src = tmp_path / "download.flac"
    src.write_bytes(b"x")
    path, dest = os.fsencode(src), os.fsencode(tmp_path / "library.flac")
    with pytest.raises(FilesystemError) as info:
        util.hardlink(path, dest)
    return info.value


def test_beets_cross_device_hardlink_is_recognised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _link_fails_with(monkeypatch, errno.EXDEV)
    assert is_cross_device_hardlink(_beets_hardlink_error(tmp_path))


def test_another_hardlink_failure_is_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: beets raises the same class for every ``link(2)`` error."""
    _link_fails_with(monkeypatch, errno.EPERM)
    assert not is_cross_device_hardlink(_beets_hardlink_error(tmp_path))


def test_a_cross_device_hardlink_job_shows_our_line_then_beets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "hardlink")
    source = _source_folder(tmp_path)
    _link_fails_with(monkeypatch, errno.EXDEV)

    run = _import(lib, source, ImportBridge())

    assert len(run.errors) == 1
    first, beets_line = run.errors[0].split("\n")
    assert first == f"CrossDeviceHardlinkError: {CROSS_DEVICE_HARDLINK}"
    assert beets_line.startswith("Cannot hard link across devices")
    assert str(source) in beets_line  # beets' own line names the download


def test_another_placement_failure_keeps_beets_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a different ``FilesystemError`` reaches the job as beets wrote it."""
    _install_lookup(monkeypatch, BeetsRec.strong)
    lib = _library(tmp_path, "hardlink")
    source = _source_folder(tmp_path)
    _link_fails_with(monkeypatch, errno.EPERM)

    run = _import(lib, source, ImportBridge())

    assert len(run.errors) == 1
    assert run.errors[0].startswith("FilesystemError: ")
    assert CROSS_DEVICE_HARDLINK not in run.errors[0]
