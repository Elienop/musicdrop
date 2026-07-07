"""The shared crash-safe atomic text-write recipe (``app.playlists.atomic``).

Pins the invariant that a secret (``mode=0o600``) is written owner-only from the
moment the tempfile is created — never a default-mode create followed by a chmod
that leaves the token bytes briefly world-readable.
"""

from __future__ import annotations

import os
import stat as stat_mod
from pathlib import Path

import pytest

from app.playlists.atomic import write_atomic_text


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

    def spy_open(path: object, flags: int, mode: int = 0o777, *args: object) -> int:
        if flags & os.O_CREAT:  # the tempfile create (the dir fsync uses O_RDONLY)
            captured.append(mode)
        return real_open(path, flags, mode, *args)  # type: ignore[arg-type]  # pass-through spy

    monkeypatch.setattr(os, "open", spy_open)
    write_atomic_text(tmp_path / "token.json", "s3cret", mode=0o600)
    assert captured == [0o600]


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
    assert list(tmp_path.glob(".playlist.m3u8.tmp*")) == []
