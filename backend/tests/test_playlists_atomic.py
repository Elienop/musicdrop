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

from app.playlists.atomic import write_atomic_bytes, write_atomic_text


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
    assert list(tmp_path.glob(".playlist.m3u8*")) == []


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
    assert list(tmp_path.glob(".p.m3u8*")) == []


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
    assert list(tmp_path.glob(".playlist.m3u8*")) == []  # both writers cleaned up
