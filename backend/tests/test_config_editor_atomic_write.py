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
* no pre-existing dst -> first-write defensive fallback to 0o644
  (``setup_beets()`` always ensures the file in production, so this
  branch is belt-and-suspenders).

The tempfile-cleanup test pins the success-path invariant: the
``.config.yaml.tmp`` sidecar must not survive a successful write,
because the ``finally`` cleanup branch + ``os.replace`` semantics
together imply it's already gone (replace consumes the tmpfile name).
"""

from __future__ import annotations

import stat as stat_mod
from pathlib import Path

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


def test_atomic_write_first_time_chmods_644(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    assert not cfg.exists()
    data = parse_yaml("a: 1\n")
    atomic_write(cfg, data, _yaml())
    assert cfg.exists()
    mode = stat_mod.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o644


def test_atomic_write_cleans_up_tmpfile_on_success(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    data = parse_yaml("a: 2\n")
    atomic_write(cfg, data, _yaml())
    tmps = list(tmp_path.glob(".config.yaml.tmp*"))
    assert tmps == []


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
