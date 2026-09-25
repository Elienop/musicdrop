"""Layer-3 config editor — atomic write tests (Plan Task 4).

``atomic_write`` hands the text to the shared writer
(``app.playlists.atomic.write_atomic_text``), so what is pinned here is this
file's policy on top of that recipe: ``dumped`` strips the YAML directive, the
parent directory is fsynced, and ``mode=None`` preserves the config's own mode
bits while letting mtime advance (``shutil.copystat`` would carry the old mtime
over and freeze the Save signal ``apply_pending`` reads).

Mode tests pin two regimes:

* pre-existing regular dst -> its mode is preserved (e.g. 0o600).
* no pre-existing dst -> first write takes the umask default (0o644 under the
  conventional umask 0o022).

Two tests pin the temp file: nothing of the target's name is derived into it
(a symlink planted at the old ``.config.yaml.tmp`` sibling was followed and
then published AS the destination), and no temp of ours survives a successful
write.
"""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path
from typing import Any

import pytest

from app.beets.config_editor import _yaml, atomic_write, dumped, parse_yaml


def test_atomic_write_writes_content(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("a: 2\nb: 3\n")
    atomic_write(cfg, dumped(data, _yaml()))
    text = cfg.read_text()
    assert "a: 2" in text
    assert "b: 3" in text


def test_atomic_write_preserves_mode(tmp_path: Path) -> None:
    """``mode=None`` keeps the existing config's mode bits (e.g. 0o600) across a
    rewrite. Only mode bits — NOT atime/mtime — see the module docstring for
    why."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    cfg.chmod(0o600)
    data = parse_yaml("a: 2\n")
    atomic_write(cfg, dumped(data, _yaml()))
    mode = stat_mod.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o600


def test_atomic_write_first_time_takes_umask_default(tmp_path: Path) -> None:
    """No pre-existing dst -> the first write takes the ``open()`` default
    under the process umask. Two scenarios (umask 0o022 -> 0o644, umask
    0o077 -> 0o600) prove the umask now governs the first-write mode."""
    data = parse_yaml("a: 1\n")

    cfg = tmp_path / "config.yaml"
    assert not cfg.exists()
    old_umask = os.umask(0o022)
    try:
        atomic_write(cfg, dumped(data, _yaml()))
    finally:
        os.umask(old_umask)
    assert cfg.exists()
    mode = stat_mod.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o644

    cfg2 = tmp_path / "config2.yaml"
    old_umask = os.umask(0o077)
    try:
        atomic_write(cfg2, dumped(data, _yaml()))
    finally:
        os.umask(old_umask)
    mode = stat_mod.S_IMODE(cfg2.stat().st_mode)
    assert mode == 0o600


def test_atomic_write_cleans_up_tmpfile_on_success(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("a: 2\n")
    atomic_write(cfg, dumped(data, _yaml()))
    # The whole directory, not a glob of one shape: the temp name is picked per
    # call, so a leftover under any shape shows up here.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml"]


def test_a_symlink_at_the_old_derived_tmp_path_is_neither_followed_nor_published(
    tmp_path: Path,
) -> None:
    """The temp's name is this call's own, so a planted one is not in the way.

    With the old ``.<dst.name>.tmp`` sibling, a link planted there was opened
    through (truncating its target) and then published AS the destination by
    ``os.replace`` — measured on the art writer: ``library.db`` overwritten
    with image bytes while the run reported the file written.
    """
    secret = tmp_path / "library.db"
    secret.write_bytes(b"the beets database")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    planted = tmp_path / f".{cfg.name}.tmp"  # exactly the path this writer used to derive
    planted.symlink_to(secret)

    atomic_write(cfg, dumped(parse_yaml("a: 2\n"), _yaml()))

    assert secret.read_bytes() == b"the beets database"  # not written through
    assert not cfg.is_symlink()  # the destination is the file itself, not the link
    assert cfg.is_file()
    assert "a: 2" in cfg.read_text()
    assert planted.is_symlink()  # left where it was, not deleted by our cleanup
    assert planted.readlink() == secret  # and still aimed at the same file
    assert sorted(p.name for p in tmp_path.iterdir()) == [planted.name, cfg.name, secret.name]


def test_atomic_write_parent_dir_fsync_open_carries_o_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PARENT-DIRECTORY fsync this module's docstring names, pinned by flag.

    Measured: a FIFO swapped in at that path makes the bare ``os.O_RDONLY`` open
    block FOREVER (still blocked after 2 s, no error), so a behavioural test
    hangs rather than failing. ``O_DIRECTORY`` fails a non-directory ENOTDIR in
    34 us, and a real directory still opens and fsyncs fine.
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    real_open = os.open
    opened: list[tuple[object, int]] = []

    # *args/**kwargs: the writer passes dir_fd= to os.open, which a
    # positional-only spy would reject with a TypeError.
    def spy_open(*args: Any, **kwargs: Any) -> int:
        if not int(args[1]) & os.O_CREAT:
            opened.append((args[0], int(args[1])))
        return real_open(*args, **kwargs)  # pass-through spy

    monkeypatch.setattr(os, "open", spy_open)
    atomic_write(cfg, dumped(parse_yaml("a: 2\n"), _yaml()))

    parent = [flags for path, flags in opened if Path(str(path)) == tmp_path]
    assert parent, "the parent directory was not fsynced through os.open"
    bare = [f"{flags:#o}" for flags in parent if not flags & os.O_DIRECTORY]
    assert not bare, f"parent-dir fsync open(s) without O_DIRECTORY: {bare}"


def test_atomic_write_omits_yaml_directive_header(tmp_path: Path) -> None:
    """``_yaml()`` sets ``version=(1,1)`` for the dump's octals and flow quoting, and ruamel
    then injects a ``%YAML 1.1\\n---\\n`` prologue on every dump —
    unrequested churn in the user's hand-edited config.yaml (diff noise, a
    changed CAS sha, a no-op save that isn't byte-identical). ``dumped``
    must strip that directive."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("# keep me\nplugins: [fetchart]\n")
    atomic_write(cfg, dumped(data, _yaml()))
    text = cfg.read_text()
    assert not text.startswith("%YAML")
    assert "%YAML" not in text
    assert "---" not in text  # the version directive drags a document-start with it
    assert "# keep me" in text  # comments still round-trip
