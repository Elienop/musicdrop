"""The delete path's stronger mount predicate: ``require_library_present``.

``require_library_root`` accepts a music root holding ANY entry, so a
``.stfolder`` (Syncthing), a ``lost+found`` or an empty leftover directory left
on a LOCAL mountpoint satisfies it while the share behind it is gone. Every
album then reads as deleted at once, and the delete path's ghost branches would
drop the library one album at a time. These tests pin the new predicate and,
just as importantly, pin that the cheap shared one was NOT made stricter — disk
sync calls it per removal and its O(1) cost is a design property.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from beets.library import Library

from app.beets import library as library_mod
from app.beets.library import (
    LibraryRootUnavailableError,
    _require_id,
    require_library_present,
    require_library_root,
)


def _music_root(lib: Library) -> Path:
    return Path(os.fsdecode(lib.directory))


def _drop_the_share(lib: Library, *, stray: str) -> Path:
    """Make the music root look like a mountpoint whose share went away.

    The share's content is gone; what remains is the local directory that was
    being mounted over, holding one entry nobody thinks of as music.
    """
    root = _music_root(lib)
    shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / stray).mkdir()
    return root


# ----- Positive control: the bug, and that the OLD predicate misses it -----


@pytest.mark.parametrize("stray", [".stfolder", "lost+found", "leftover-empty-dir"])
def test_stray_entry_on_a_dropped_mountpoint(duplicates_lib: Library, stray: str) -> None:
    """The dropped share the cheap predicate cannot see, and the new one can.

    Both halves are asserted in one test on purpose: "the new check rejects it"
    is only meaningful beside "the old check accepted it" — separately, either
    could pass for the wrong reason.
    """
    root = _drop_the_share(duplicates_lib, stray=stray)
    assert list(root.iterdir()) == [root / stray], "the root must be non-empty but musicless"

    require_library_root(duplicates_lib)  # accepts it — this is the bug

    with pytest.raises(LibraryRootUnavailableError) as ei:
        require_library_present(duplicates_lib)
    assert "holds none of the library's albums" in str(ei.value)


def test_refusal_message_leaks_no_path(duplicates_lib: Library) -> None:
    """User-facing text stays actionable and names no filesystem path."""
    root = _drop_the_share(duplicates_lib, stray=".stfolder")

    with pytest.raises(LibraryRootUnavailableError) as ei:
        require_library_present(duplicates_lib)

    message = str(ei.value)
    assert str(root) not in message
    assert os.sep not in message
    assert "Is the music share mounted?" in message


# ----- Negative control: healthy / empty / legitimately-deleted must pass -----


def test_healthy_library_passes(duplicates_lib: Library) -> None:
    require_library_present(duplicates_lib)  # must not raise


def test_genuinely_empty_library_is_not_locked_out(empty_lib: Library) -> None:
    """No rows means no folder that could be missing — nothing to prove.

    The root itself still has to look mounted, so the root predicate's own empty
    arm fires first; give it one entry and the presence check has to concede.
    """
    root = _music_root(empty_lib)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".stfolder").mkdir()

    require_library_present(empty_lib)  # must not raise


def test_legitimately_deleted_album_is_still_deletable(duplicates_lib: Library) -> None:
    """The album under deletion is gone from disk; its siblings are not.

    This is the ghost the delete path exists to clean up. A predicate that
    refused here would make a ghost row permanently undeletable.
    """
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    folder = os.path.dirname(os.fsdecode(next(iter(album.items())).path))
    shutil.rmtree(folder)

    require_library_present(duplicates_lib)  # must not raise


def test_a_single_surviving_album_is_enough(duplicates_lib: Library) -> None:
    """Every album but one deleted outside MusicDrop — still not a dropped share.

    ``_PRESENCE_SAMPLE_SIZE`` is 5 and the fixture holds 5 albums, so this is the
    worst case the sample can face: 4 of 5 draws miss.
    """
    keep = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    keep_id = _require_id(keep.id)
    for album in duplicates_lib.albums():
        if _require_id(album.id) == keep_id:
            continue
        shutil.rmtree(os.path.dirname(os.fsdecode(next(iter(album.items())).path)))

    require_library_present(duplicates_lib)  # must not raise


def test_singleton_only_library_is_verified_too(empty_lib: Library) -> None:
    """A library that groups nothing has zero album rows; its items still count.

    Without the singleton fallback the album query returns nothing, "no sample"
    reads as "nothing to verify", and a whole class of library silently loses the
    guard.
    """
    from beets.library import Item

    root = _music_root(empty_lib)
    (root / "Loose").mkdir(parents=True)
    track = root / "Loose" / "01 Track.mp3"
    track.write_bytes(b"\x00")
    item = Item(title="Track", artist="Nobody")
    item.path = os.fsencode(str(track))
    empty_lib.add(item)

    require_library_present(empty_lib)  # the folder is there

    shutil.rmtree(root)
    root.mkdir()
    (root / ".stfolder").mkdir()
    with pytest.raises(LibraryRootUnavailableError):
        require_library_present(empty_lib)


# ----- The second invariant: the SHARED default must not have got stricter -----


def test_require_library_root_never_samples_the_database(
    duplicates_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """disk-sync's per-removal predicate must not reach the sampler.

    Booby-trap the sampler rather than counting queries: this fails loudly if
    anyone ever "unifies" the two predicates, which is precisely the regression
    the split exists to prevent.
    """

    def _must_not_run(*_args: object, **_kwargs: object) -> list[str]:
        raise AssertionError("require_library_root sampled the DB — it must stay O(1)")

    monkeypatch.setattr(library_mod, "_sampled_library_dirs", _must_not_run)

    require_library_root(duplicates_lib)  # must not raise


def test_require_library_root_still_costs_one_isdir_and_one_scandir(
    duplicates_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its documented O(1) cost, measured — one entry, never a recursive scan."""
    calls: dict[str, int] = {"isdir": 0, "scandir": 0}
    real_isdir = os.path.isdir
    real_scandir = os.scandir

    def _isdir(path: Any) -> bool:
        calls["isdir"] += 1
        return real_isdir(path)

    def _scandir(path: Any) -> Any:
        calls["scandir"] += 1
        return real_scandir(path)

    monkeypatch.setattr(os.path, "isdir", _isdir)
    monkeypatch.setattr(os, "scandir", _scandir)

    require_library_root(duplicates_lib)

    assert calls == {"isdir": 1, "scandir": 1}


def test_require_library_present_short_circuits_on_the_first_hit(
    duplicates_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy library pays ONE sample stat, not ``_PRESENCE_SAMPLE_SIZE``.

    The sample budget is spent only on the refusal path; that is what makes the
    check affordable inside a fan-out.
    """
    checked: list[str] = []
    real_isdir = os.path.isdir

    def _isdir(path: Any) -> bool:
        checked.append(str(path))
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", _isdir)

    require_library_present(duplicates_lib)

    # One for the root guard, one for the first sampled album folder.
    assert len(checked) == 2, checked


# ----- SQLite is dynamically typed: a raw `path` row is not always BLOB -----


def _rewrite_every_path_as_text(lib: Library) -> None:
    """Store every item's ``path`` as TEXT, byte-for-byte the same path.

    What an external tool or a hand-run ``UPDATE items SET path = '...'`` leaves
    behind: the column is declared BLOB, but SQLite stores what it was given and
    hands it back with the type it was written with. beets' own model API never
    notices, because it reads through a converter — only the raw-SQL readers do.
    """
    with lib.transaction() as tx:
        rows = tx.query("SELECT id, path FROM items")
        for row in rows:
            tx.mutate("UPDATE items SET path = ? WHERE id = ?", (os.fsdecode(row[1]), row[0]))
        assert {r[0] for r in tx.query("SELECT DISTINCT typeof(path) FROM items")} == {"text"}


def test_the_presence_check_survives_a_path_row_stored_as_text(
    duplicates_lib: Library,
) -> None:
    """``bytes(raw)`` raised ``TypeError`` on a str; ``os.fsencode`` takes both.

    Fails CLOSED, which is what makes it worth a test rather than a shrug: the
    TypeError escaped from the one predicate whose whole job is answering "is the
    music really there" before a delete drops rows, so a delete 500'd instead of
    taking its ghost branch and a restore 500'd instead of answering 503.
    """
    _rewrite_every_path_as_text(duplicates_lib)

    require_library_present(duplicates_lib)  # the raise under test is TypeError, not the 503

    # And the paths still resolve to the same folders, so the check is not
    # merely surviving — it is still asking the real question.
    dirs = library_mod._sampled_library_dirs(duplicates_lib, 5)
    assert dirs, "a seeded library must sample something"
    assert all(os.path.isdir(d) for d in dirs)


def test_a_text_path_row_does_not_hide_a_dropped_share(duplicates_lib: Library) -> None:
    """The TEXT row must not turn the refusal into a pass either.

    Pairs with the test above the way the dropped-share pair at the top of this
    file does: "it no longer crashes" is only worth having beside "it still
    refuses", or a fix that swallowed the row entirely would pass both halves of
    the first test.
    """
    _rewrite_every_path_as_text(duplicates_lib)
    _drop_the_share(duplicates_lib, stray=".stfolder")

    with pytest.raises(LibraryRootUnavailableError):
        require_library_present(duplicates_lib)
