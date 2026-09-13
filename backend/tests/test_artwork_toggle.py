import json
import os
import re
import stat
from pathlib import Path

import pytest

from app.artwork.toggle import ArtistImageToggle


def test_default_used_when_no_file(tmp_path: Path) -> None:
    t = ArtistImageToggle(tmp_path / "_enabled.json", default=True)
    assert t.is_enabled() is True


def test_set_enabled_flips_in_memory(tmp_path: Path) -> None:
    t = ArtistImageToggle(tmp_path / "_enabled.json", default=False)
    assert t.is_enabled() is False
    assert t.set_enabled(True) is True
    assert t.is_enabled() is True


def test_persisted_value_wins_over_default_on_reload(tmp_path: Path) -> None:
    path = tmp_path / "_enabled.json"
    ArtistImageToggle(path, default=False).set_enabled(True)
    # A fresh instance with the opposite env default must still read True.
    assert ArtistImageToggle(path, default=False).is_enabled() is True


def test_corrupt_file_falls_back_to_default(tmp_path: Path) -> None:
    path = tmp_path / "_enabled.json"
    path.write_text("not json", encoding="utf-8")
    assert ArtistImageToggle(path, default=True).is_enabled() is True


def test_non_bool_value_falls_back_to_default(tmp_path: Path) -> None:
    path = tmp_path / "_enabled.json"
    path.write_text('{"enabled": "yes"}', encoding="utf-8")
    assert ArtistImageToggle(path, default=False).is_enabled() is False


def test_set_creates_missing_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "_enabled.json"
    ArtistImageToggle(path, default=False).set_enabled(True)
    assert path.exists()


def test_a_symlink_at_the_old_derived_tmp_name_is_neither_followed_nor_published(
    tmp_path: Path,
) -> None:
    """The write used to derive ``.<name>.tmp`` and open it following links.

    Measured on the art writer: ``os.replace`` then published the LINK as the
    destination. Here the planted link points at a secret beside the dir.
    """
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"do not touch")
    path = tmp_path / "cache" / "_enabled.json"
    path.parent.mkdir()
    planted = path.parent / f".{path.name}.tmp"
    planted.symlink_to(secret)

    assert ArtistImageToggle(path, default=False).set_enabled(True) is True

    assert secret.read_bytes() == b"do not touch"
    assert planted.is_symlink()
    assert os.readlink(planted) == str(secret)
    assert not path.is_symlink()
    assert stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    assert json.loads(path.read_text(encoding="utf-8")) == {"enabled": True}


def test_the_write_is_fsynced_and_leaves_only_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behaviour change: this file was published with no fsync at all.

    The created temp is pinned NOT to embed the target's name, so nobody can
    precompute the name the exclusive create opens.
    """
    path = tmp_path / "_enabled.json"
    created: list[str] = []
    real_open = os.open
    real_fsync = os.fsync
    fsynced: list[int] = []

    def spy_open(name: object, flags: int, *args: object, **kwargs: object) -> int:
        if flags & os.O_CREAT:
            created.append(os.path.basename(os.fsdecode(name)))  # type: ignore[arg-type]  # str|bytes
        return real_open(name, flags, *args, **kwargs)  # type: ignore[arg-type]  # spy passthrough

    def spy_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    ArtistImageToggle(path, default=False).set_enabled(True)
    monkeypatch.undo()

    assert len(fsynced) == 2, "the file's own fd, then the parent directory's"
    assert len(created) == 1, created
    assert re.fullmatch(r"\.\d+\.[0-9a-f]{16}\.json\.tmp", created[0]), created[0]
    assert path.name not in created[0]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["_enabled.json"]


def test_a_tightened_toggle_file_keeps_its_mode_on_rewrite(tmp_path: Path) -> None:
    """``mode=None``: the owner-only mode an operator set survives a flip."""
    path = tmp_path / "_enabled.json"
    ArtistImageToggle(path, default=False).set_enabled(True)
    path.chmod(0o600)

    ArtistImageToggle(path, default=False).set_enabled(False)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"enabled": False}
