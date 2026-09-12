"""Layer-3 config editor — atomic write tests (Plan Task 4).

Pins the canonical atomic-write recipe from Dan Luu's *Files are hard*
(danluu.com/file-consistency/) + the LWN ext4-rename discussion
(lwn.net/Articles/322823/): tmpfile in the same dir, fsync the tmpfile,
``copymode`` from dst (mode bits only — atime/mtime intentionally NOT
preserved so the Save signal advances ``apply_pending``; ``shutil.copystat``
would carry the old mtime over and freeze the freshness check, so we use
``shutil.copymode`` instead), ``os.replace``, then fsync the PARENT
DIRECTORY (otherwise the rename can be lost on power-cut even on ext4).
The ``atomicwrites`` PyPI package was deprecated by its own author in
favor of this recipe (see github.com/untitaker/python-atomicwrites), so
we roll it ourselves.

Mode tests pin two regimes:

* pre-existing dst -> ``copymode`` preserves the user's mode (e.g. 0o600).
* no pre-existing dst -> first write takes the ``open()`` default under
  the process umask (typically 0o644 under the conventional umask 0o022).

The tempfile-cleanup test pins the success-path invariant: the
``.config.yaml.tmp`` sidecar must not survive a successful write,
because the ``finally`` cleanup branch + ``os.replace`` semantics
together imply it's already gone (replace consumes the tmpfile name).
"""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path
from typing import Any

import pytest

from app.beets.config_editor import _yaml, atomic_write, parse_yaml


def test_atomic_write_writes_content(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("a: 2\nb: 3\n")
    atomic_write(cfg, data, _yaml())
    text = cfg.read_text()
    assert "a: 2" in text
    assert "b: 3" in text


def test_atomic_write_preserves_mode(tmp_path: Path) -> None:
    """``copymode`` carries the user's mode bits (e.g. 0o600) from dst -> tmp
    so the post-replace file keeps the same permissions. Only mode bits — NOT
    atime/mtime — see the module docstring for why."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    cfg.chmod(0o600)
    data = parse_yaml("a: 2\n")
    atomic_write(cfg, data, _yaml())
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
        atomic_write(cfg, data, _yaml())
    finally:
        os.umask(old_umask)
    assert cfg.exists()
    mode = stat_mod.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o644

    cfg2 = tmp_path / "config2.yaml"
    old_umask = os.umask(0o077)
    try:
        atomic_write(cfg2, data, _yaml())
    finally:
        os.umask(old_umask)
    mode = stat_mod.S_IMODE(cfg2.stat().st_mode)
    assert mode == 0o600


def test_atomic_write_cleans_up_tmpfile_on_success(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("a: 2\n")
    atomic_write(cfg, data, _yaml())
    tmps = list(tmp_path.glob(".config.yaml.tmp*"))
    assert tmps == []


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
    atomic_write(cfg, parse_yaml("a: 2\n"), _yaml())

    parent = [flags for path, flags in opened if Path(str(path)) == tmp_path]
    assert parent, "the parent directory was not fsynced through os.open"
    bare = [f"{flags:#o}" for flags in parent if not flags & os.O_DIRECTORY]
    assert not bare, f"parent-dir fsync open(s) without O_DIRECTORY: {bare}"


def test_atomic_write_omits_yaml_directive_header(tmp_path: Path) -> None:
    """``_yaml()`` sets ``version=(1,1)`` so ``yes``/``no`` parse as bool, but
    ruamel then also injects a ``%YAML 1.1\\n---\\n`` prologue on every dump —
    unrequested churn in the user's hand-edited config.yaml (diff noise, a
    changed CAS sha, a no-op save that isn't byte-identical). ``atomic_write``
    must strip that directive; the version stays a LOAD-side concern only."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("# keep me\nplugins: [fetchart]\n")
    atomic_write(cfg, data, _yaml())
    text = cfg.read_text()
    assert not text.startswith("%YAML")
    assert "%YAML" not in text
    assert "---" not in text  # the version directive drags a document-start with it
    assert "# keep me" in text  # comments still round-trip
