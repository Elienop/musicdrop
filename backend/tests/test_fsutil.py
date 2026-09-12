"""The ENAMETOOLONG guard for fs predicates on request-supplied paths, and
:func:`app.fsutil.occupied`'s own answer where the two differ.

Contract: a name-shaped failure (kernel: "File name too long") answers as
"does not exist" so unknown-name refusal paths work; EVERY other OSError is
re-raised — swallowing a permission or mount failure would turn a real
incident into a silent 404.

Plus :func:`app.fsutil.open_below`: the descent from the music root that refuses
a symlinked component below it, and the errno the kernel actually answers.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

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


def _ident(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def _ident_of(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _spy_os_open(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every ``os.open`` path and pass it through, so a refusal that did
    not happen is visible as an fd the walk really opened."""
    calls: list[object] = []
    real_open = os.open

    def spy(*args: Any, **kwargs: Any) -> int:  # widened: os.open is overloaded
        calls.append(args[0])
        return int(real_open(*args, **kwargs))

    monkeypatch.setattr(os, "open", spy)
    return calls


def test_a_symlinked_component_below_the_root_is_refused(tmp_path: Path) -> None:
    """The errno is MEASURED, not taken from ``open(2)``.

    ``O_DIRECTORY|O_NOFOLLOW`` on a symlink answers ENOTDIR (20) here, not the
    documented ELOOP — ELOOP needs ``O_NOFOLLOW`` without ``O_DIRECTORY``.
    """
    root = tmp_path / "music"
    (root / "Real").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (root / "Link").symlink_to(tmp_path / "outside", target_is_directory=True)

    before = _open_fds()
    with pytest.raises(OSError) as excinfo:
        fsutil.open_below(root, Path("Link"))
    assert excinfo.value.errno == errno.ENOTDIR
    assert _open_fds() == before, "the root fd the walk opened is closed on the refusal"

    fd = fsutil.open_below(root, Path("Real"))  # control: the real sibling opens
    try:
        assert _ident(fd) == _ident_of(root / "Real")
    finally:
        os.close(fd)


def test_a_symlinked_root_is_followed(tmp_path: Path) -> None:
    """An operator's beets ``directory:`` may be a link; only parts BELOW it are refused."""
    real = tmp_path / "elsewhere"
    (real / "Artist").mkdir(parents=True)
    root = tmp_path / "music"
    root.symlink_to(real, target_is_directory=True)

    fd = fsutil.open_below(root, Path("Artist"))
    try:
        assert _ident(fd) == _ident_of(real / "Artist")
    finally:
        os.close(fd)


def test_open_root_follows_a_link_at_the_root(tmp_path: Path) -> None:
    """The one spelling of the root open the anchored callers share. An
    operator's beets ``directory:`` may be a link, so this one FOLLOWS."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    root = tmp_path / "music"
    root.symlink_to(real, target_is_directory=True)

    fd = fsutil.open_root(root)
    try:
        assert _ident(fd) == _ident_of(real)
    finally:
        os.close(fd)


def test_open_root_refuses_a_fifo_instead_of_blocking_on_it(tmp_path: Path) -> None:
    """``O_DIRECTORY`` is mandatory, not tidy: measured, a FIFO at the root
    answers ENOTDIR with it and BLOCKS the open for as long as no writer appears
    without it. This test completing at all is the no-hang half."""
    root = tmp_path / "music"
    os.mkfifo(root)

    with pytest.raises(OSError) as err:
        fsutil.open_root(root)

    assert err.value.errno == errno.ENOTDIR


@pytest.mark.parametrize("rel", ["..", "../outside", "Artist/../../outside", ".", ""])
def test_a_climbing_or_empty_rel_is_refused_before_any_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str
) -> None:
    """``Path("")`` and ``Path(".")`` both carry ZERO parts (measured), so one
    refusal answers both; ``..`` is the part pathlib keeps."""
    root = tmp_path / "music"
    (root / "Artist").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    calls = _spy_os_open(monkeypatch)
    with pytest.raises(ValueError):
        fsutil.open_below(root, Path(rel))
    assert calls == [], "refused before the root was even opened"


def test_an_absolute_rel_is_refused_before_any_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    calls = _spy_os_open(monkeypatch)
    with pytest.raises(ValueError):
        fsutil.open_below(root, Path("/etc"))
    assert calls == []


def test_a_deep_path_returns_the_leaf_and_leaks_no_intermediate_fd(tmp_path: Path) -> None:
    root = tmp_path / "music"
    leaf = root / "Artist" / "Album" / "Disc 1"
    leaf.mkdir(parents=True)

    before = _open_fds()
    fd = fsutil.open_below(root, Path("Artist/Album/Disc 1"))
    try:
        assert _ident(fd) == _ident_of(leaf)
        assert _open_fds() == before + 1, "only the returned fd is still open"
    finally:
        os.close(fd)
    assert _open_fds() == before


def test_the_link_between_two_real_components_is_refused_at_the_link(tmp_path: Path) -> None:
    """The case a leaf-only ``O_NOFOLLOW`` clears.

    Measured: ``os.open("<root>/A/link/C", O_DIRECTORY|O_NOFOLLOW)`` — the flag on
    the whole path, which only tests the LEAF — SUCCEEDS. Per component it is
    refused at ``link``, and the OSError names that part.
    """
    root = tmp_path / "music"
    (root / "B" / "C").mkdir(parents=True)
    (root / "A").mkdir()
    (root / "A" / "link").symlink_to(Path("..") / "B", target_is_directory=True)

    leaf_only = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(str(root / "A" / "link" / "C"), leaf_only)
    os.close(fd)  # the weaker check the walk replaces: it opened

    with pytest.raises(OSError) as excinfo:
        fsutil.open_below(root, Path("A/link/C"))
    assert excinfo.value.errno == errno.ENOTDIR
    assert excinfo.value.filename == "link", "refused at the link, not at the leaf"


def test_a_nul_in_a_part_refuses_without_leaking_the_walked_fd(tmp_path: Path) -> None:
    """``os.open`` answers a NUL with ValueError, not OSError — so the fd cleanup
    on the walk cannot be an ``except OSError``."""
    root = tmp_path / "music"
    (root / "Artist").mkdir(parents=True)

    before = _open_fds()
    with pytest.raises(ValueError):
        fsutil.open_below(root, Path("Artist/a\x00b"))
    assert _open_fds() == before
