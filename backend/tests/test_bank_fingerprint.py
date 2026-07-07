"""The fingerprint must be stable across runs and sensitive to content changes."""

import os
from pathlib import Path

import pytest

from app.bank.fingerprint import folder_fingerprint


def _make_album(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "01 - one.mp3").write_bytes(b"aaaa")
    (folder / "02 - two.mp3").write_bytes(b"bbbbbb")
    (folder / "cover.jpg").write_bytes(b"img")


def test_fingerprint_stable(tmp_path: Path) -> None:
    _make_album(tmp_path / "Album")
    first = folder_fingerprint(tmp_path / "Album")
    second = folder_fingerprint(tmp_path / "Album")
    assert first == second
    assert len(first) == 64  # sha256 hex


def test_fingerprint_changes_on_added_file(tmp_path: Path) -> None:
    _make_album(tmp_path / "Album")
    before = folder_fingerprint(tmp_path / "Album")
    (tmp_path / "Album" / "03 - three.mp3").write_bytes(b"cc")
    assert folder_fingerprint(tmp_path / "Album") != before


def test_fingerprint_changes_on_size_change(tmp_path: Path) -> None:
    _make_album(tmp_path / "Album")
    before = folder_fingerprint(tmp_path / "Album")
    (tmp_path / "Album" / "01 - one.mp3").write_bytes(b"aaaa-grown")
    assert folder_fingerprint(tmp_path / "Album") != before


def test_fingerprint_changes_on_mtime_change(tmp_path: Path) -> None:
    _make_album(tmp_path / "Album")
    before = folder_fingerprint(tmp_path / "Album")
    target = tmp_path / "Album" / "02 - two.mp3"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert folder_fingerprint(tmp_path / "Album") != before


def test_fingerprint_ignores_stray_sidecars(tmp_path: Path) -> None:
    # Non-audio files written into a banked folder AFTER banking (cover art by
    # Plex, .DS_Store by macOS, Thumbs.db by Windows/SMB, .lrc/.txt sidecars)
    # must NOT change the fingerprint: only the audio identifies the album, so a
    # stray write can't wrongly stale the row on apply.
    album = tmp_path / "Album"
    album.mkdir(parents=True)
    (album / "01 - one.mp3").write_bytes(b"aaaa")
    before = folder_fingerprint(album)
    (album / "cover.jpg").write_bytes(b"img")
    (album / ".DS_Store").write_bytes(b"junk")
    (album / "Thumbs.db").write_bytes(b"junk")
    (album / "01 - one.lrc").write_bytes(b"[00:00.00]la")
    (album / "notes.txt").write_bytes(b"hi")
    assert folder_fingerprint(album) == before


def test_fingerprint_audio_suffix_is_case_insensitive(tmp_path: Path) -> None:
    # Uppercase-extension audio still counts toward identity (the suffix compare
    # is case-insensitive): adding a second audio file changes the fingerprint.
    album = tmp_path / "Album"
    album.mkdir(parents=True)
    (album / "01 - one.FLAC").write_bytes(b"aaaa")
    before = folder_fingerprint(album)
    (album / "02 - two.MP3").write_bytes(b"bb")
    assert folder_fingerprint(album) != before


def test_fingerprint_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        folder_fingerprint(tmp_path / "gone")
