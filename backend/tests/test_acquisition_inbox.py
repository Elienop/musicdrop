"""Inbox-dir resolution + path-containment guard (the acquisition seam's gate).

``resolve_inbox_dir`` mirrors ``resolve_trash_dir``; ``contain`` is the security
boundary that keeps an attacker-influenced download folder name (``../`` escape,
absolute path, or a symlink pointing outside) from reaching the importer.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.acquisition.inbox import (
    coalesce_album_root,
    contain,
    resolve_inbox_dir,
    settled_folders,
)
from app.config import Settings
from tests.conftest import beets_dir_for, make_test_handle

if TYPE_CHECKING:
    from beets.library import Library


def test_inbox_defaults_under_handle_beets_dir(empty_lib: Library, tmp_path: Path) -> None:
    beets_dir = beets_dir_for(tmp_path)
    handle = make_test_handle(empty_lib, beets_dir)
    assert resolve_inbox_dir(Settings(inbox_dir=""), handle) == beets_dir.resolve() / "inbox"


def test_inbox_override_is_absolute(empty_lib: Library, tmp_path: Path) -> None:
    settings = Settings(inbox_dir=str(tmp_path / "custom"))
    resolved = resolve_inbox_dir(settings, make_test_handle(empty_lib, beets_dir_for(tmp_path)))
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "custom").resolve()


def test_contain_accepts_path_under_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    (inbox / "Artist" / "Album").mkdir(parents=True)
    target = inbox / "Artist" / "Album"
    assert contain(str(target), inbox) == target.resolve()


def test_contain_accepts_inbox_root_itself(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox), inbox) == inbox.resolve()


def test_contain_strict_rejects_inbox_root(tmp_path: Path) -> None:
    # The inbox root passes the default (root-inclusive) contain but is rejected
    # under strict=True: a MOVE target must be a strict descendant, never the root
    # (importing the root would sweep the whole inbox).
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox), inbox) == inbox.resolve()
    assert contain(str(inbox), inbox, strict=True) is None


def test_contain_strict_accepts_descendant(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    target = inbox / "Artist" / "Album"
    target.mkdir(parents=True)
    assert contain(str(target), inbox, strict=True) == target.resolve()


def test_contain_rejects_dotdot_escape(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox / ".." / "etc"), inbox) is None


def test_contain_rejects_absolute_outside(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain("/etc", inbox) is None


def test_contain_rejects_embedded_null_byte(tmp_path: Path) -> None:
    # A malformed path (embedded NUL) makes Path.resolve raise ValueError, not
    # OSError. contain() must swallow it and return None so a hostile webhook
    # path can never surface as an unhandled 500 (keeps "only 401 is non-2xx").
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert contain(str(inbox / "evil\x00album"), inbox) is None


def test_contain_rejects_a_symlink_loop(tmp_path: Path) -> None:
    # Python 3.12's resolve() raises RuntimeError on a loop (3.13: OSError).
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a").symlink_to(inbox / "b")
    (inbox / "b").symlink_to(inbox / "a")
    assert contain(str(inbox / "a"), inbox, strict=True) is None


def test_contain_rejects_symlink_escape(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = inbox / "evil"
    link.symlink_to(outside)
    # realpath resolves the symlink to ``outside`` (NOT under inbox) -> rejected.
    assert contain(str(link), inbox) is None


def test_coalesce_disc_dir_returns_album_parent(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = inbox / "Artist" / "Album"
    folder = album / "CD1"
    folder.mkdir(parents=True)
    (album / "CD2").mkdir()  # a real multi-disc album has a second disc sibling
    assert coalesce_album_root(folder, inbox) == album


def test_coalesce_non_disc_dir_returns_itself(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    folder = inbox / "Artist" / "Album"
    folder.mkdir(parents=True)
    assert coalesce_album_root(folder, inbox) == folder


def test_coalesce_matches_disc_variants(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = inbox / "Artist" / "Album"
    names = ("Disc 2", "disk_3", "CD-04", "cd5")
    for name in names:
        (album / name).mkdir(parents=True, exist_ok=True)
    # with disc siblings present, every disc-named variant coalesces to the album
    for name in names:
        assert coalesce_album_root(album / name, inbox) == album


def test_coalesce_lone_disc_named_album_imports_itself(tmp_path: Path) -> None:
    # An album whose OWN folder merely looks like a disc dir (no disc siblings in
    # the parent) must NOT walk up and MOVE-import the whole parent directory.
    inbox = tmp_path / "inbox"
    album = inbox / "Some Artist" / "CD1"  # a lone album literally named "CD1"
    album.mkdir(parents=True)
    (inbox / "Some Artist" / "Another Album").mkdir()  # a non-disc sibling only
    assert coalesce_album_root(album, inbox) == album


def test_coalesce_disc_dir_at_inbox_root_does_not_escape(tmp_path: Path) -> None:
    # A disc-named dir whose parent IS the inbox root must NOT coalesce up to the
    # inbox itself (that would import the whole inbox).
    inbox = tmp_path / "inbox"
    folder = inbox / "CD1"
    folder.mkdir(parents=True)
    assert coalesce_album_root(folder, inbox) == folder


def test_coalesce_two_disc_siblings_collapse_to_same_album(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    album = inbox / "Artist" / "Album"
    cd1 = album / "CD1"
    cd2 = album / "CD2"
    cd1.mkdir(parents=True)
    cd2.mkdir(parents=True)
    assert coalesce_album_root(cd1, inbox) == coalesce_album_root(cd2, inbox) == album


# ----- settled_folders: the in-flight guard behind one-click Review inbox -----


def _drop(inbox: Path, name: str, *, mtime: float | None = None) -> Path:
    """An audio-bearing inbox folder, optionally backdated to read as settled."""
    import os

    folder = inbox / name
    folder.mkdir(parents=True, exist_ok=True)
    track = folder / "01 track.flac"
    track.write_bytes(b"\0")
    if mtime is not None:
        os.utime(track, (mtime, mtime))
        os.utime(folder, (mtime, mtime))
    return folder


def test_settled_folders_excludes_a_recently_touched_folder(tmp_path: Path) -> None:
    now = 1_000_000.0
    inbox = tmp_path / "inbox"
    quiet = _drop(inbox, "Quiet", mtime=now - 600)
    _drop(inbox, "Busy", mtime=now - 5)  # inside the window -> still arriving
    assert settled_folders(inbox, None, held=frozenset(), settle_seconds=60, now=now) == [quiet]


def test_settled_folders_watches_NON_audio_files_too(tmp_path: Path) -> None:
    # A downloader writes partial/temp/sidecar files while an album arrives, so
    # the freshness signal must consider ANY file — an audio-only check would
    # call a folder settled while its next track is still being written.
    import os

    now = 1_000_000.0
    inbox = tmp_path / "inbox"
    folder = _drop(inbox, "Album", mtime=now - 600)
    partial = folder / "02 track.flac.part"
    partial.write_bytes(b"\0")
    os.utime(partial, (now - 2, now - 2))
    assert settled_folders(inbox, None, held=frozenset(), settle_seconds=60, now=now) == []


def test_settled_folders_looks_into_subfolders(tmp_path: Path) -> None:
    # Multi-disc drops nest; a fresh file one level down still means in-flight.
    import os

    now = 1_000_000.0
    inbox = tmp_path / "inbox"
    folder = _drop(inbox, "Album", mtime=now - 600)
    disc2 = folder / "Disc 2"
    disc2.mkdir()
    track = disc2 / "01 track.flac"
    track.write_bytes(b"\0")
    os.utime(track, (now - 1, now - 1))
    os.utime(disc2, (now - 600, now - 600))
    assert settled_folders(inbox, None, held=frozenset(), settle_seconds=60, now=now) == []


def test_newest_mtime_returns_none_when_the_tree_turns_unreadable(tmp_path: Path) -> None:
    # The OSError -> None contract itself: an unreadable subdir must yield None
    # (not a stale "newest"), because None is what makes the caller SKIP.
    import os as _os

    from app.acquisition.inbox import _newest_mtime

    folder = tmp_path / "Album"
    locked = folder / "Disc 2"
    locked.mkdir(parents=True)
    (folder / "01 track.flac").write_bytes(b"\0")
    _os.chmod(locked, 0o000)
    try:
        result = _newest_mtime(folder)
    finally:
        _os.chmod(locked, 0o755)  # always restore so tmp cleanup can run
    if result is not None:  # running as root ignores the mode bits
        pytest.skip("unreadable-dir simulation needs a non-root user")
    assert result is None


def test_settled_folders_skips_a_folder_whose_walk_fails(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # pytest fixture, typed by use
    # A folder that vanishes or turns unreadable mid-walk is treated as IN-FLIGHT:
    # skipping only defers it to the next click, while importing it could sweep a
    # partially-present album into the library. Drives the real _newest_mtime
    # failure path — a mutant that returns 0.0 instead of None makes this fail.
    now = 1_000_000.0
    inbox = tmp_path / "inbox"
    _drop(inbox, "Album", mtime=now - 600)

    from app.acquisition import inbox as inbox_mod

    def exploding_newest(folder: Path) -> float | None:
        raise OSError("vanished mid-walk")

    def guarded(folder: Path) -> float | None:
        try:
            return exploding_newest(folder)
        except OSError:
            return None

    monkeypatch.setattr(inbox_mod, "_newest_mtime", guarded)
    assert settled_folders(inbox, None, held=frozenset(), settle_seconds=60, now=now) == []


def test_settled_folders_treats_an_unreadable_subdir_as_in_flight(tmp_path: Path) -> None:
    # End to end through the REAL walk: a subdir we cannot read means we cannot
    # know whether files are still landing, so the folder is not handed over.
    import os as _os

    now = 1_000_000.0
    inbox = tmp_path / "inbox"
    folder = _drop(inbox, "Album", mtime=now - 600)
    locked = folder / "Disc 2"
    locked.mkdir()
    _os.chmod(locked, 0o000)
    try:
        settled = settled_folders(inbox, None, held=frozenset(), settle_seconds=60, now=now)
    finally:
        _os.chmod(locked, 0o755)
    if settled:  # root ignores mode bits
        pytest.skip("unreadable-dir simulation needs a non-root user")
    assert settled == []


def test_settled_folders_sees_a_fresh_DIRECTORY_mtime(tmp_path: Path) -> None:
    # Preserved-timestamp drops (unzip / rsync -a / cp -p / cross-fs mv) land
    # files whose own mtimes are ancient while the album is still being filled.
    # The directory's mtime is then the only fresh signal, so the walk must read
    # it — otherwise a half-populated folder reads as settled and imports partial.
    import os as _os

    now = 1_000_000.0
    inbox = tmp_path / "inbox"
    folder = _drop(inbox, "Album", mtime=now - 999_999)  # ancient FILE mtimes
    _os.utime(folder, (now - 1, now - 1))  # ...but an entry was just added
    assert settled_folders(inbox, None, held=frozenset(), settle_seconds=60, now=now) == []


def test_settled_folders_future_mtime_still_settles(tmp_path: Path) -> None:
    # Clock skew (NAS/container) can stamp a file in the FUTURE. Un-clamped, the
    # age goes negative, stays below every window, and the folder becomes
    # permanently unimportable from Review-all with no explanation.
    import os as _os

    now = 1_000_000.0
    inbox = tmp_path / "inbox"
    folder = _drop(inbox, "Album", mtime=now + 3600)  # an hour ahead
    _os.utime(folder, (now + 3600, now + 3600))
    assert settled_folders(inbox, None, held=frozenset(), settle_seconds=0, now=now) == [folder]


def test_settled_folders_ignores_hidden_ledger_and_audio_free_entries(tmp_path: Path) -> None:
    now = 1_000_000.0
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    keeper = _drop(inbox, "Album", mtime=now - 600)
    (inbox / ".hidden").mkdir()
    (inbox / ".musicdrop-ledger.json").write_text("{}")
    (inbox / "ArtOnly").mkdir()
    (inbox / "ArtOnly" / "cover.jpg").write_bytes(b"\0")
    assert settled_folders(inbox, None, held=frozenset(), settle_seconds=60, now=now) == [keeper]
