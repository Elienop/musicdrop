"""The shared crash-safe atomic text-write recipe (``app.playlists.atomic``).

Pins the invariant that a secret (``mode=0o600``) is written owner-only from the
moment the tempfile is created — never a default-mode create followed by a chmod
that leaves the token bytes briefly world-readable.
"""

from __future__ import annotations

import os
import re
import stat as stat_mod
import time
from pathlib import Path
from typing import Any

import pytest

from app.playlists.atomic import write_atomic_bytes, write_atomic_text

#: The temp shape this writer must use, spelled out here rather than imported:
#: an independent pin, so a change to the module's own constant cannot make its
#: test agree with it. ``.<pid>.<16 hex><suffix>.tmp``, no target name in it.
TMP_SHAPE = re.compile(r"^\.\d+\.[0-9a-f]{16}(\.[^./]*)?\.tmp$")


def test_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "playlist.m3u8"
    write_atomic_text(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_secret_tempfile_is_created_with_the_restrictive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tempfile is created 0o600 UP FRONT — the create carries the mode, so
    there is no create->chmod window where the secret is world-readable."""
    captured: list[int] = []
    real_open = os.open

    def spy_open(*args: Any, **kwargs: Any) -> int:
        if int(args[1]) & os.O_CREAT:  # the tempfile create (the dir fsync uses O_RDONLY)
            captured.append(int(args[2]))
        return real_open(*args, **kwargs)  # pass-through spy

    monkeypatch.setattr(os, "open", spy_open)
    write_atomic_text(tmp_path / "token.json", "s3cret", mode=0o600)
    assert captured == [0o600]


def test_the_parent_dir_fsync_open_carries_o_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dir open in the tail of the recipe, which every caller of this shared
    primitive inherits.

    A flag pin, not a behavioural one: measured, a FIFO swapped in at that path
    makes the bare ``os.O_RDONLY`` open block FOREVER (still blocked after 2 s,
    no error), so the behavioural version hangs instead of going red.
    ``O_DIRECTORY`` fails a non-directory ENOTDIR in 34 us.
    """
    real_open = os.open
    opened: list[tuple[object, int]] = []

    def spy_open(*args: Any, **kwargs: Any) -> int:
        if not int(args[1]) & os.O_CREAT:
            opened.append((args[0], int(args[1])))
        return real_open(*args, **kwargs)  # pass-through spy

    monkeypatch.setattr(os, "open", spy_open)
    write_atomic_text(tmp_path / "playlist.m3u8", "hello\n")

    parent = [flags for path, flags in opened if Path(str(path)) == tmp_path]
    assert parent, "the parent directory was not fsynced through os.open"
    bare = [f"{flags:#o}" for flags in parent if not flags & os.O_DIRECTORY]
    assert not bare, f"parent-dir fsync open(s) without O_DIRECTORY: {bare}"


def test_secret_final_file_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    write_atomic_text(target, "s3cret", mode=0o600)
    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o600


def test_default_final_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "playlist.m3u8"
    write_atomic_text(target, "data")
    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o644


def test_tempfile_cleaned_up_on_success(tmp_path: Path) -> None:
    target = tmp_path / "playlist.m3u8"
    write_atomic_text(target, "data")
    # The whole directory, not a glob of the old name-embedding shape: the temp
    # name is unpredictable now, so only "nothing but the target" can see it.
    assert sorted(tmp_path.iterdir()) == [target]


def test_text_writer_stays_strict_utf8(tmp_path: Path) -> None:
    """The TEXT wrapper must keep rejecting lone surrogates. The JSON stores and
    config writers rely on strict UTF-8; only the ``.m3u8`` export may write
    surrogate-carrying content, and it does so through the bytes sink with its
    own ``surrogateescape`` encode — never by loosening this wrapper."""
    with pytest.raises(UnicodeEncodeError):
        write_atomic_text(tmp_path / "t.json", "Caf\udce9")
    assert not (tmp_path / "t.json").exists()


def test_bytes_writes_content(tmp_path: Path) -> None:
    target = tmp_path / "p.m3u8"
    write_atomic_bytes(target, b"hello\n")
    assert target.read_bytes() == b"hello\n"


def test_bytes_writes_undecodable_bytes_verbatim(tmp_path: Path) -> None:
    """The bytes sink is encoding-agnostic: raw bytes land exactly as given."""
    target = tmp_path / "p.m3u8"
    write_atomic_bytes(target, b"Caf\xe9/track.mp3\n")
    assert target.read_bytes() == b"Caf\xe9/track.mp3\n"


def test_bytes_default_final_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "p.m3u8"
    write_atomic_bytes(target, b"data")
    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o644


def test_bytes_explicit_final_file_mode(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    write_atomic_bytes(target, b"s3cret", mode=0o600)
    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o600


def test_bytes_tempfile_cleaned_up_on_success(tmp_path: Path) -> None:
    target = tmp_path / "p.m3u8"
    write_atomic_bytes(target, b"data")
    assert sorted(tmp_path.iterdir()) == [target]


def test_bytes_replace_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "p.m3u8"
    target.write_bytes(b"old")
    write_atomic_bytes(target, b"new")
    assert target.read_bytes() == b"new"


def test_overlapping_writers_never_corrupt_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent writers of the SAME target must not share one tmp inode.

    With a fixed ``.<name>.tmp`` name, writer B's ``O_TRUNC`` wipes writer A's
    just-written bytes and A's ``os.replace`` then either publishes truncated
    content or raises on the vanished tmp — the ``.m3u8`` export self-corrupts.
    Orchestrate the exact interleave deterministically: hook ``os.fsync`` so that
    the instant writer A has written its tmp (but not yet replaced), a COMPLETE
    overlapping write B runs. A must still finish cleanly and the final file must
    be one whole write, never a truncated/mixed blend.
    """
    target = tmp_path / "playlist.m3u8"
    a_text = "A" * 20_000
    b_text = "B" * 20_000
    real_fsync = os.fsync
    state = {"nested": False}

    def hook_fsync(fd: int) -> None:
        if not state["nested"]:
            state["nested"] = True
            write_atomic_text(target, b_text)  # writer B, fully, mid-flight of A
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", hook_fsync)
    write_atomic_text(target, a_text)  # writer A — must not raise despite B racing

    final = target.read_text(encoding="utf-8")
    assert final in (a_text, b_text)  # a COMPLETE write won
    assert len(set(final)) == 1  # homogeneous — never truncated or mixed
    assert sorted(tmp_path.iterdir()) == [target]  # both writers cleaned up


# ----- the temp file: exclusive, unguessable, and not named after the target -----


def test_the_temp_is_created_exclusively_and_without_following_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flag pin. ``O_EXCL`` refuses an existing path instead of opening it and
    ``O_NOFOLLOW`` refuses a symlink; measured on the art writer next door, a
    followed link at the temp path let ``os.replace`` publish the LINK as the
    destination — ``library.db`` overwritten with JPEG bytes."""
    created: list[int] = []
    real_open = os.open

    def spy(*args: Any, **kwargs: Any) -> int:
        flags = int(args[1])
        if flags & os.O_CREAT:
            created.append(flags)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    write_atomic_bytes(tmp_path / "p.m3u8", b"data")

    assert created, "the temp file was not created through os.open"
    assert created[0] & os.O_EXCL
    assert created[0] & os.O_NOFOLLOW


def test_the_temp_name_is_unpredictable_and_does_not_embed_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two properties in one shape: nothing can precompute the name (so nothing
    can squat it), and the decoration does not grow with the target's name — the
    old ``.<name>.<pid>.<hex>.tmp`` cost ``len(name) + 30`` bytes and pushed
    long names over NAME_MAX on the temp while the target itself was legal."""
    names: list[str] = []
    real_open = os.open

    def spy(*args: Any, **kwargs: Any) -> int:
        if int(args[1]) & os.O_CREAT:
            names.append(str(args[0]))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    write_atomic_bytes(tmp_path / "Radiohead - In Rainbows.m3u8", b"data")
    write_atomic_bytes(tmp_path / ("L" * 180 + ".m3u8"), b"data")

    assert len(names) == 2
    assert all(TMP_SHAPE.match(name) for name in names), names
    assert "Radiohead" not in names[0]
    assert len(os.fsencode(names[0])) == len(os.fsencode(names[1]))


# ----- the publish is anchored to a descriptor, not to a path -----


def test_the_publish_lands_in_the_directory_the_descriptor_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory is opened once and every name is resolved against that fd,
    so swapping the directory out mid-write cannot move the publish.

    Deterministic interleave: hook ``os.fsync`` so the instant the temp's bytes
    are on disk (before the ``os.replace``) the directory is renamed away and a
    decoy takes its place. A publish by PATH would land in the decoy.
    """
    real_dir = tmp_path / "d"
    real_dir.mkdir()
    real_fsync = os.fsync
    state = {"swapped": False}

    def hook_fsync(fd: int) -> None:
        if not state["swapped"]:
            state["swapped"] = True
            os.rename(real_dir, tmp_path / "moved")
            real_dir.mkdir()  # the decoy a path-based publish would write into
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", hook_fsync)
    write_atomic_bytes(real_dir / "p.m3u8", b"data")

    assert state["swapped"]
    assert (tmp_path / "moved" / "p.m3u8").read_bytes() == b"data"
    assert list(real_dir.iterdir()) == []


def test_a_caller_held_dir_fd_is_never_reopened_by_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the keyword: a caller that opened the directory safely keeps
    that guarantee. Assert the ABSENCE of an open of ``path.parent`` — the temp
    create through the fd is allowed to appear — and that nothing is created."""
    folder = tmp_path / "library" / "Artist"
    folder.mkdir(parents=True)
    seen: list[str] = []
    made: list[str] = []
    real_open, real_mkdir = os.open, os.mkdir

    def spy_open(*args: Any, **kwargs: Any) -> int:
        seen.append(str(args[0]))
        return real_open(*args, **kwargs)

    def spy_mkdir(*args: Any, **kwargs: Any) -> None:
        made.append(str(args[0]))
        real_mkdir(*args, **kwargs)

    fd = real_open(folder, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "mkdir", spy_mkdir)
    try:
        write_atomic_bytes(folder / "artist-poster.png", b"\x89PNG", dir_fd=fd)
    finally:
        os.close(fd)

    assert [p for p in seen if Path(p) == folder] == []
    assert made == []
    assert (folder / "artist-poster.png").read_bytes() == b"\x89PNG"


# ----- mode: an int, or "keep what the regular file already had" -----


def test_mode_none_takes_the_umask_default_on_a_first_write(tmp_path: Path) -> None:
    """Moved here from the art/lyrics suites, which own the ``mode=None``
    callers: a new sidecar or poster must be readable by the Plex process."""
    target = tmp_path / "artist-poster.png"
    old_umask = os.umask(0o022)
    try:
        write_atomic_bytes(target, b"\x89PNG", mode=None)
    finally:
        os.umask(old_umask)
    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o644


def test_mode_none_preserves_a_tightened_mode_on_rewrite(tmp_path: Path) -> None:
    """The other half of the same regime: a rewrite must not reset a mode an
    operator tightened back to the umask default via the temp file."""
    target = tmp_path / "t.txt"
    write_atomic_text(target, "first\n", mode=None)
    target.chmod(0o600)
    write_atomic_text(target, "second\n", mode=None)
    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == "second\n"


def test_the_mode_is_pinned_against_the_umask(tmp_path: Path) -> None:
    """``os.open`` honours the umask, so the create alone cannot guarantee the
    mode: under umask 0o077 a ``mode=0o644`` file lands 0o600 and a preserved
    0o646 loses its group/other bits. The fchmod on the temp's own fd is what
    makes the published mode exact, in both regimes."""
    target = tmp_path / "p.m3u8"
    keep = tmp_path / "t.txt"
    keep.write_bytes(b"first")
    keep.chmod(0o646)

    old_umask = os.umask(0o077)
    try:
        write_atomic_bytes(target, b"data", mode=0o644)
        write_atomic_bytes(keep, b"second", mode=None)
    finally:
        os.umask(old_umask)

    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o644
    assert stat_mod.S_IMODE(keep.stat().st_mode) == 0o646


def test_a_symlink_at_the_target_is_replaced_by_a_regular_file(tmp_path: Path) -> None:
    """A link at the destination is replaced, its target left alone — and it
    donates no mode under ``mode=None``, because the stat behind that regime does
    not follow it (a followed link would hand over its target's 0o600)."""
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"untouched")
    outside.chmod(0o600)
    target = tmp_path / "p.m3u8"
    target.symlink_to(outside)

    old_umask = os.umask(0o022)
    try:
        write_atomic_bytes(target, b"data", mode=None)
    finally:
        os.umask(old_umask)

    assert not target.is_symlink()
    assert target.read_bytes() == b"data"
    assert outside.read_bytes() == b"untouched"
    assert stat_mod.S_IMODE(target.stat().st_mode) == 0o644


def test_a_directory_at_the_target_refuses_the_publish_and_leaves_no_temp(tmp_path: Path) -> None:
    """``os.replace`` cannot put a file where a directory is (EISDIR, measured),
    and the failure arm clears the temp through the same descriptor."""
    target = tmp_path / "p.m3u8"
    target.mkdir()
    (target / "inside.txt").write_bytes(b"kept")

    with pytest.raises(IsADirectoryError):
        write_atomic_bytes(target, b"data")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["p.m3u8"]
    assert (target / "inside.txt").read_bytes() == b"kept"


# ----- the stale-temp sweep -----


def _backdate(path: Path, *, hours: float) -> None:
    when = time.time() - hours * 3600
    os.utime(path, (when, when), follow_symlinks=False)


def test_the_sweep_removes_only_this_writers_own_stale_temps(tmp_path: Path) -> None:
    """The residual this closes: a process KILLED mid-write leaves a dotfile no
    later call cleared. Only an OLD REGULAR file in exactly this writer's shape
    goes; every other neighbour stays.
    """
    stale = tmp_path / ".1234.0123456789abcdef.m3u8.tmp"
    stale.write_bytes(b"abandoned")
    _backdate(stale, hours=2)

    in_flight = tmp_path / ".1234.fedcba9876543210.m3u8.tmp"
    in_flight.write_bytes(b"another writer, right now")

    not_ours = tmp_path / ".notes.tmp"
    not_ours.write_bytes(b"someone else's temp")
    _backdate(not_ours, hours=2)

    pointed_at = tmp_path / "keepme.txt"
    pointed_at.write_bytes(b"a real file")
    _backdate(pointed_at, hours=2)  # so a stat that FOLLOWED would pass the age gate
    link = tmp_path / ".1234.00000000000000ff.m3u8.tmp"
    link.symlink_to(pointed_at)
    _backdate(link, hours=2)

    a_dir = tmp_path / ".1234.aaaaaaaaaaaaaaaa.m3u8.tmp"
    a_dir.mkdir()
    _backdate(a_dir, hours=2)

    target = tmp_path / "p.m3u8"
    write_atomic_bytes(target, b"data")

    assert not stale.exists()
    assert in_flight.read_bytes() == b"another writer, right now"
    assert not_ours.read_bytes() == b"someone else's temp"
    assert link.is_symlink()
    assert pointed_at.read_bytes() == b"a real file"
    assert a_dir.is_dir()
    assert target.read_bytes() == b"data"


def test_a_write_that_swept_still_publishes_through_the_same_dir_fd(tmp_path: Path) -> None:
    """The sweep enumerates through the write's own directory descriptor, and an
    fd scandir shares that fd's offset: a PARTIALLY consumed iterator left alive
    makes the next enumeration of the fd read ``[]`` (measured; an exhausted
    iterator closes itself). So the caller's fd must come back usable."""
    stale = tmp_path / ".1234.0123456789abcdef.m3u8.tmp"
    stale.write_bytes(b"abandoned")
    _backdate(stale, hours=2)
    for name in ("a.m3u8", "b.m3u8"):
        (tmp_path / name).write_bytes(b"x")

    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        write_atomic_bytes(tmp_path / "p.m3u8", b"data", dir_fd=fd)
        after = sorted(os.listdir(fd))
    finally:
        os.close(fd)

    assert not stale.exists(), "the sweep removed nothing, so this proves nothing"
    assert after == ["a.m3u8", "b.m3u8", "p.m3u8"]
