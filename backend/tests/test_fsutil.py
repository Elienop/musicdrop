"""The ENAMETOOLONG guard for fs predicates on request-supplied paths, and
:func:`app.fsutil.occupied`'s own answer where the two differ.

Contract: a name-shaped failure (kernel: "File name too long") answers as
"does not exist" so unknown-name refusal paths work; EVERY other OSError is
re-raised — swallowing a permission or mount failure would turn a real
incident into a silent 404.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from app import fsutil


def _force_fail(monkeypatch: pytest.MonkeyPatch, predicate: str, errno_val: int) -> None:
    def boom(_self: Path) -> bool:
        raise OSError(errno_val, "forced failure")

    monkeypatch.setattr(Path, predicate, boom)


def test_exists_maps_enametoolong_to_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_fail(monkeypatch, "exists", errno.ENAMETOOLONG)
    assert fsutil.exists(tmp_path / "anything") is False


def test_is_dir_maps_enametoolong_to_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_fail(monkeypatch, "is_dir", errno.ENAMETOOLONG)
    assert fsutil.is_dir(tmp_path / "anything") is False


def test_exists_reraises_other_oserrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_fail(monkeypatch, "exists", errno.EACCES)
    with pytest.raises(OSError) as excinfo:
        fsutil.exists(tmp_path / "anything")
    assert excinfo.value.errno == errno.EACCES


def test_is_dir_reraises_other_oserrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_fail(monkeypatch, "is_dir", errno.ESTALE)
    with pytest.raises(OSError) as excinfo:
        fsutil.is_dir(tmp_path / "anything")
    assert excinfo.value.errno == errno.ESTALE


def test_real_overlong_path_reads_as_missing(tmp_path: Path) -> None:
    # Kernel-level: a component over NAME_MAX (255 bytes) cannot exist, and the
    # raw pathlib predicates raise on it (CPython only swallows ENOENT/ENOTDIR/
    # EBADF/ELOOP) — the guarded ones answer "no" so the caller's refusal path
    # runs.
    overlong = tmp_path / ("x" * 300)
    with pytest.raises(OSError):
        overlong.exists()  # the unguarded predicate is unsafe
    assert fsutil.exists(overlong) is False
    assert fsutil.is_dir(overlong) is False


def test_existing_paths_stay_reachable(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    assert fsutil.exists(real) is True
    assert fsutil.is_dir(real) is True


def test_a_symlink_to_an_empty_directory_is_occupied(tmp_path: Path) -> None:
    """The ``is_symlink`` tripwire, which the callers below it would hide.

    An EMPTY directory is deliberately NOT occupied — ``rename`` replaces one,
    and refusing there would strand every album whose folder a pruning beets or
    a half-finished sync left behind. A LINK to an empty directory looks the
    same to ``is_dir`` and ``scandir``, which both follow it, so without the
    ``is_symlink`` test first this answers "free" for a path that leaves the
    music library entirely.

    Nothing in the app acts on that answer differently today — every move below
    refuses a symlink anyway — so this is the predicate's ANSWER being pinned,
    not a caller's behaviour. Measured with the clause removed: ``occupied``
    answers False here.
    """
    target = tmp_path / "somewhere else"
    target.mkdir()
    link = tmp_path / "Album"
    link.symlink_to(target, target_is_directory=True)

    assert fsutil.occupied(link) is True, "following it would leave the music library"
    assert fsutil.occupied(target) is False, "...while the empty directory itself is free"
