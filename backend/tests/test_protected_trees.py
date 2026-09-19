"""The identity guard the movers and the remover run at the point of destruction.

``store_layout`` compares SPELLINGS, and a bind mount gives one directory two
of them. The namespace test below measures both halves in one child process:
``check_store_layout`` ALLOWS the aliased layout, and with the guard's set empty
— which is what this tree looked like before — ``empty_all`` deletes the beets
dir through it.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
import stat
from collections import Counter
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.beets import delete as delete_mod
from app.beets.library import _require_id
from app.beets.protected import (
    ProtectedTreeError,
    ProtectedTrees,
    open_checked_dir,
    protected_match,
    protected_trees,
    refuse_protected_tree,
)
from app.beets.trash import trash_folder
from app.beets.trash_manage import TrashEmptyPartialError, empty_all, empty_one
from app.config import Settings, app_owned_dirs, settings
from tests._mountns import run_probe, unshare_works
from tests.conftest import (
    beets_dir_for,
    build_library,
    library_with_no_rows,
    make_test_handle,
    origins_for,
    protected_for,
)

# --------------------------------------------------------------------------
# The set itself: one owner for every app-owned path.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("configured", ["", "  "])
def test_a_store_setting_the_app_ignores_names_the_default_here_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """The guard and the resolver read ONE function, so they answer alike.

    They read two, and the copies diverged on a whitespace-only value: the guard
    stripped and named ``<B>/inbox`` while ``resolve_inbox_dir`` tested
    truthiness and named ``<cwd>/'  '`` — measured, so the guard protected a
    directory the app was not using. ``config.store_dir`` is the owner now;
    this is the value that showed the split.
    """
    from app.acquisition.inbox import resolve_inbox_dir

    beets_dir = (tmp_path / "beets").resolve()
    beets_dir.mkdir()
    monkeypatch.setattr("app.config.settings.inbox_dir", configured)

    class _Handle:
        pass

    handle = _Handle()
    handle.beets_dir = beets_dir  # type: ignore[attr-defined]  # only field inbox reads

    mine = {name: path for path, name, _setting in app_owned_dirs(settings, beets_dir)}
    assert mine["the inbox"] == beets_dir / "inbox"
    assert resolve_inbox_dir(settings, handle) == beets_dir / "inbox"  # type: ignore[arg-type]  # ditto


def test_every_directory_the_rule_is_about_joins_the_set(tmp_path: Path) -> None:
    """The membership list itself, one row at a time.

    Without this, dropping any single entry from ``protected_trees`` leaves the
    suite green — measured: removing the playlist-exports row survived the whole
    file. ``trash`` is asserted beside them because ``empty_all`` fstat-compares
    against it and a ``None`` there turns that compare off.

    The two image caches are here because they were absent: a Trash pointed at
    either of them booted clean and the first Empty Trash removed the cache
    directory itself, measured in the review round.
    """
    dirs = {
        name: tmp_path / name.replace(" ", "-")
        for name in (
            "the music library",
            "the beets data directory",
            "the Trash directory",
            "the Trash origin store",
            "the beets database's folder",
            "the playlist exports",
            "the import bank",
            "the Plex settings store",
            "the slskd settings store",
            "the playlist store",
            "the inbox",
            "the artist-image cache",
            "the cover-thumbnail cache",
        )
    }
    for path in dirs.values():
        path.mkdir()
    trees = protected_trees(
        settings=Settings(
            playlists_export_dir=str(dirs["the playlist exports"]),
            bank_dir=str(dirs["the import bank"]),
            plex_settings_dir=str(dirs["the Plex settings store"]),
            slskd_settings_dir=str(dirs["the slskd settings store"]),
            playlists_dir=str(dirs["the playlist store"]),
            inbox_dir=str(dirs["the inbox"]),
            artist_image_cache_dir=str(dirs["the artist-image cache"]),
            cover_thumb_cache_dir=str(dirs["the cover-thumbnail cache"]),
        ),
        music_dir=dirs["the music library"],
        beets_dir=dirs["the beets data directory"],
        trash_dir=dirs["the Trash directory"],
        origins_dir=dirs["the Trash origin store"],
        library_path=dirs["the beets database's folder"] / "library.db",
    )

    assert {name for name, _setting in trees.ids.values()} == set(dirs)
    for name, path in dirs.items():
        st = os.stat(path)
        assert trees.ids[(st.st_dev, st.st_ino)][0] == name
    trash_st = os.stat(dirs["the Trash directory"])
    assert trees.trash == (trash_st.st_dev, trash_st.st_ino)


def test_checked_protected_trees_reads_the_music_root_and_db_off_the_handle(
    tmp_path: Path,
) -> None:
    """The two the OPEN LIBRARY owns, rather than anything re-resolved.

    The route tests reach this through a configured inbox, so ``music_dir`` and
    ``library_path`` could both be wrong there and still refuse — measured, both
    replaced by ``handle.beets_dir`` left every use-site test green. The
    database is put in a third directory so its folder and the beets dir do not
    share an identity and mask each other.
    """
    from app.beets.store_layout import checked_protected_trees
    from tests.conftest import build_library

    music = tmp_path / "music"
    music.mkdir()
    beets_dir = beets_dir_for(tmp_path)
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    trash = tmp_path / "trash"
    trash.mkdir()
    lib = build_library(str(db_dir / "library.db"), str(music))
    handle = make_test_handle(lib, beets_dir)

    trees = checked_protected_trees(
        Settings(), handle, trash_dir=trash, origins_dir=origins_for(trash)
    )
    named = {path: trees.ids[_ident_of(path)][0] for path in (music, beets_dir, db_dir, trash)}
    assert named == {
        music: "the music library",
        beets_dir: "the beets data directory",
        db_dir: "the beets database's folder",
        trash: "the Trash directory",
    }


def _ident_of(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def test_a_path_that_is_not_there_yet_has_no_identity(tmp_path: Path) -> None:
    """An uncreated store drops out rather than matching everything.

    The complement of the walk: a ``None`` identity must not collapse into a
    key several absent paths share, which is what makes the guard silent on a
    fresh install where ``bank/``, ``plex/`` and ``inbox/`` do not exist yet.
    """
    beets = tmp_path / "beets"
    beets.mkdir()
    trees = protected_trees(
        # The two cache dirs default to the repo root, where a dev checkout
        # really has them; point them at absent paths so the set is empty.
        settings=Settings(
            artist_image_cache_dir=str(tmp_path / "gone-art"),
            cover_thumb_cache_dir=str(tmp_path / "gone-thumbs"),
        ),
        music_dir=tmp_path / "gone-music",
        beets_dir=beets,
        trash_dir=tmp_path / "gone-trash",
        origins_dir=tmp_path / "gone-origins",
        library_path=tmp_path / "gone" / "library.db",
    )
    assert [name for name, _setting in trees.ids.values()] == ["the beets data directory"]
    assert trees.trash is None


# --------------------------------------------------------------------------
# The walk.
# --------------------------------------------------------------------------


def _add_album(lib: Any, folder: Path) -> None:
    """One two-track album whose files sit in ``folder``."""
    from beets.library import Item

    items = []
    for i in (1, 2):
        track = folder / f"{i:02d} Track.mp3"
        track.write_bytes(b"\x00")
        item = Item(album="Kid A", albumartist="Radiohead", artist="Radiohead", track=i)
        item.path = os.fsencode(str(track))
        items.append(item)
    lib.add_album(items)


def _add_multi_disc_album(lib: Any, root: Path) -> None:
    """One album with a track in each of ``root``'s ``CD1``/``CD2`` folders.

    So the album's root is the COMMONPATH of two item directories and is not any
    item's own directory — the shape a question about the first item's folder
    cannot see.
    """
    from beets.library import Item

    items = []
    for i, disc in enumerate(("CD1", "CD2"), 1):
        track = root / disc / f"{i:02d} Track.mp3"
        track.write_bytes(b"\x00")
        item = Item(
            album="Kid A",
            albumartist="Radiohead",
            artist="Radiohead",
            track=i,
            disc=i,
            title=f"T{i}",
        )
        item.path = os.fsencode(str(track))
        items.append(item)
    lib.add_album(items)


def _trees_for(music: Path, trash: Path | None = None) -> ProtectedTrees:
    """A set holding exactly ``music``, so a test's assertion is about one id.

    ``trash`` is passed whenever the test drives either delete path: both open
    the Trash through ``open_checked_dir``, which refuses an identity it could
    not take rather than skipping the compare.
    """
    return protected_trees(
        settings=Settings(),
        music_dir=music,
        beets_dir=music.parent / "absent-beets",
        trash_dir=trash if trash is not None else music.parent / "absent-trash",
        origins_dir=music.parent / "absent-origins",
        library_path=music.parent / "absent" / "library.db",
    )


def _trees_with_trash(trash: Path) -> ProtectedTrees:
    """A set whose ``trash`` identity is real and whose ``ids`` hold nothing."""
    return protected_trees(
        settings=Settings(),
        music_dir=trash.parent / "absent-music",
        beets_dir=trash.parent / "absent-beets",
        trash_dir=trash,
        origins_dir=trash.parent / "absent-origins",
        library_path=trash.parent / "absent" / "library.db",
    )


def test_the_walk_finds_a_protected_directory_below_the_root(tmp_path: Path) -> None:
    """ "contains", not just "is" — the loss is a whole-tree move or rmtree."""
    music = tmp_path / "music"
    (music / "Artist" / "Album").mkdir(parents=True)
    holder = tmp_path / "holder" / "deep"
    holder.mkdir(parents=True)
    os.rename(music, holder / "music")
    moved = holder / "music"
    trees = _trees_for(moved)

    assert protected_match(tmp_path / "holder", trees) == (
        "contains the music library (same inode as `directory:` in config.yaml)"
    )
    assert protected_match(moved, trees) == (
        "is the music library (same inode as `directory:` in config.yaml)"
    )
    assert protected_match(moved / "Artist", trees) is None


def test_a_symlinked_root_is_not_walked(tmp_path: Path) -> None:
    """The callers move or unlink the LINK, so what it points at is not at risk.

    Measured the other way as the control: the same directory reached by its own
    name is refused, so this is the link being spared and not the guard being off.
    """
    music = tmp_path / "music"
    music.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(music, target_is_directory=True)
    trees = _trees_for(music)

    assert protected_match(link, trees) is None
    assert protected_match(music, trees) is not None


def test_a_directory_the_guard_cannot_read_is_logged_and_not_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ "I could not look" leaves Trash emptiable, and says so once in the log.

    The opposite trade from ``find_orphan_folders``, which errs toward keeping:
    refusing here would turn one permission bit into a Trash no route can clear.
    The residual is stated in the BACKLOG entry for this slice.
    """
    if os.getuid() == 0:
        pytest.skip("root reads a mode-000 directory anyway")
    entry = tmp_path / "entry"
    (entry / "locked").mkdir(parents=True)
    os.chmod(entry / "locked", 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="app.beets.protected"):
            assert protected_match(entry, _trees_for(tmp_path / "music")) is None
    finally:
        os.chmod(entry / "locked", 0o755)
    assert "could not read" in caplog.text


def test_empty_all_carries_on_past_an_entry_it_cannot_open(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable ROOT is the same trade as an unreadable sub-directory.

    ``os.fwalk`` hands a sub-directory to ``onerror`` and RE-RAISES for the root,
    so the guard used to abort the whole sweep on the first mode-000 entry: the
    count was lost, every later entry stayed, and the route answered 500. The
    entry's own identity is compared from the Trash's descriptor either way.
    """
    if os.getuid() == 0:
        pytest.skip("root reads a mode-000 directory anyway")
    trash = tmp_path / "trash"
    for name in ("a", "b", "c"):
        (trash / name).mkdir(parents=True)
        (trash / name / "01.flac").write_bytes(b"x")
    os.chmod(trash / "b", 0o000)
    trees = _trees_for(tmp_path / "music", trash)
    origins = origins_for(trash)
    lib = library_with_no_rows(tmp_path)

    try:
        with caplog.at_level(logging.WARNING, logger="app.beets.protected"):
            with pytest.raises(TrashEmptyPartialError) as caught:
                empty_all(trash, origins_dir=origins, protected=trees, lib=lib)
    finally:
        os.chmod(trash / "b", 0o755)

    assert "removed 2 of 3" in str(caught.value)
    assert "'b'" in str(caught.value)
    assert "could not read" in caplog.text
    assert not (trash / "a").exists()
    assert not (trash / "c").exists()


# --------------------------------------------------------------------------
# The remover's pinned descriptor.
# --------------------------------------------------------------------------


def test_open_checked_dir_refuses_a_symlink_at_the_trash_path(tmp_path: Path) -> None:
    """``O_NOFOLLOW``: the swap the 0.115-0.260 ms window allowed.

    ``iterdir()`` followed such a link and ``rmtree``'d the children of whatever
    it pointed at; this refuses before a name is read.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "trash"
    link.symlink_to(real, target_is_directory=True)
    trees = _trees_with_trash(link)
    with pytest.raises(ProtectedTreeError, match="is not the directory MusicDrop checked"):
        open_checked_dir(link, trees)


def test_open_checked_dir_refuses_an_identity_that_moved(tmp_path: Path) -> None:
    """A directory replaced by a DIFFERENT one keeps the path and loses the inode.

    The impostor is made while the original still holds its inode, then renamed
    over it. ``rmdir`` then ``mkdir`` at the same path hands the new directory
    the freed inode on ext4 — the identity matched, nothing was refused, and this
    passed locally while failing on CI.
    """
    trash = tmp_path / "trash"
    trash.mkdir()
    trees = _trees_with_trash(trash)
    impostor = tmp_path / "impostor"
    impostor.mkdir()
    checked = trash.stat()
    assert impostor.stat().st_ino != checked.st_ino, "the impostor must be a different directory"
    trash.rmdir()
    impostor.rename(trash)
    with pytest.raises(ProtectedTreeError, match="changed between the check and the open"):
        open_checked_dir(tmp_path / "trash", trees)


def test_empty_all_refuses_a_trash_directory_swapped_after_the_check(tmp_path: Path) -> None:
    """It must pass the CHECKED identity, not ``None``.

    ``open_checked_dir`` refuses either way when the path became a symlink, so
    only a swap for a different real DIRECTORY tells the two apart — measured:
    with ``protected.trash`` replaced by ``None`` the rest of this file stayed
    green. Nothing in the impostor is removed.
    """
    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    trees = _trees_with_trash(trash)
    os.rename(trash, tmp_path / "gone")
    (trash / "Impostor").mkdir(parents=True)
    origins = origins_for(trash)
    lib = library_with_no_rows(tmp_path)

    with pytest.raises(ProtectedTreeError, match="changed between the check and the open"):
        empty_all(trash, origins_dir=origins, protected=trees, lib=lib)
    assert (trash / "Impostor").is_dir()


def test_empty_all_enumerates_from_the_descriptor_it_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window itself: the Trash is REPLACED between the ``open`` and the loop.

    Forced deterministically by wrapping ``open_checked_dir``. Every name is
    enumerated, guarded, stat'd and removed through THAT descriptor, so the run
    acts on the tree the check approved (``Album``, now at ``gone/``) and the
    directory now standing at the Trash path is not touched. Building
    ``trash_dir / name`` per entry instead deleted ``Decoy`` and left ``Album``.
    """
    import app.beets.trash_manage as manage

    trash = tmp_path / "trash"
    (trash / "Album" / "art.jpg").parent.mkdir(parents=True)
    (trash / "Album" / "art.jpg").write_bytes(b"x")
    decoy = tmp_path / "decoy"
    (decoy / "Decoy").mkdir(parents=True)
    real_open = open_checked_dir

    def swapping(path: Path, protected: ProtectedTrees) -> int:
        fd = real_open(path, protected)
        os.rename(path, tmp_path / "gone")
        os.rename(decoy, path)
        return fd

    monkeypatch.setattr(manage, "open_checked_dir", swapping)
    result = empty_all(
        trash,
        origins_dir=origins_for(trash),
        protected=_trees_for(tmp_path / "music", trash),
        lib=library_with_no_rows(tmp_path),
    )

    assert result.removed == 1
    assert not (tmp_path / "gone" / "Album").exists(), "the entry the check approved"
    assert (trash / "Decoy").is_dir(), "the swapped-in directory"


# --------------------------------------------------------------------------
# The remover and the movers.
# --------------------------------------------------------------------------


def test_empty_all_leaves_the_protected_entry_and_removes_the_rest(tmp_path: Path) -> None:
    """One entry refused is not the whole request refused, and the 503 names it."""
    trash = tmp_path / "trash"
    (trash / "Ordinary").mkdir(parents=True)
    (trash / "Ordinary" / "01.flac").write_bytes(b"x")
    music = tmp_path / "music"
    music.mkdir()
    os.rename(music, trash / "Sneak")
    trees = _trees_for(trash / "Sneak", trash)
    origins = origins_for(trash)

    lib = library_with_no_rows(tmp_path)

    with pytest.raises(ProtectedTreeError) as caught:
        empty_all(trash, origins_dir=origins, protected=trees, lib=lib)

    assert "'Sneak' is the music library" in str(caught.value)
    assert "Removed 1" in str(caught.value)
    assert (trash / "Sneak").is_dir()
    assert not (trash / "Ordinary").exists()


def test_two_refused_entries_read_as_two(tmp_path: Path) -> None:
    """Plural wording, and the entries that could not be removed by name.

    The sentence said "move that entry out of Trash" over two of them, and the
    stuck entry was a bare count while the partial twin names them.
    """
    if os.getuid() == 0:
        pytest.skip("root removes a mode-000 entry anyway")
    trash = tmp_path / "trash"
    for name in ("bank", "inbox", "ordinary", "stuck"):
        (trash / name).mkdir(parents=True)
    (trash / "stuck" / "01.flac").write_bytes(b"x")
    os.chmod(trash / "stuck", 0o500)
    trees = protected_trees(
        settings=Settings(bank_dir=str(trash / "bank"), inbox_dir=str(trash / "inbox")),
        music_dir=tmp_path / "absent-music",
        beets_dir=tmp_path / "absent-beets",
        trash_dir=trash,
        origins_dir=tmp_path / "absent-origins",
        library_path=tmp_path / "absent" / "library.db",
    )
    origins = origins_for(trash)
    lib = library_with_no_rows(tmp_path)

    try:
        with pytest.raises(ProtectedTreeError) as caught:
            empty_all(trash, origins_dir=origins, protected=trees, lib=lib)
    finally:
        os.chmod(trash / "stuck", 0o700)

    message = str(caught.value)
    assert "move those entries out of Trash" in message, message
    assert "1 could not be removed ('stuck')" in message, message


def test_all_three_causes_reach_the_user_in_the_one_message(tmp_path: Path) -> None:
    """A sweep can leave entries behind for three reasons, and raise ONCE.

    The listed cause outranks the other two, so the raise it makes is the only
    message the user sees: an entry the guard refused, or one that could not be
    removed, was invisible on every retry. Whole string, because a fragment could
    not see a clause going missing.
    """
    if os.getuid() == 0:
        pytest.skip("root removes a mode-000 entry anyway")
    trash = tmp_path / "trash"
    music = tmp_path / "music"
    music.mkdir()
    listed = trash / "AAA-listed"
    listed.mkdir(parents=True)
    (trash / "MMM-ordinary").mkdir()
    stuck = trash / "YYY-stuck"
    stuck.mkdir()
    (stuck / "01.flac").write_bytes(b"x")
    os.chmod(stuck, 0o500)
    lib = build_library(str(beets_dir_for(tmp_path) / "library.db"), str(music))
    _add_album(lib, listed)
    # The app's own inbox, inside Trash: the guard refuses it by inode, which it
    # takes when the set is built — so the directory exists first.
    (trash / "ZZZ-inbox").mkdir()
    trees = protected_trees(
        settings=Settings(inbox_dir=str(trash / "ZZZ-inbox")),
        music_dir=music,
        beets_dir=beets_dir_for(tmp_path),
        trash_dir=trash,
        origins_dir=origins_for(trash),
        library_path=beets_dir_for(tmp_path) / "library.db",
    )

    origins = origins_for(trash)

    try:
        with pytest.raises(ProtectedTreeError) as caught:
            empty_all(trash, origins_dir=origins, protected=trees, lib=lib)
    finally:
        os.chmod(stuck, 0o700)

    assert str(caught.value) == (
        "Refused: 'AAA-listed' — the library still lists files inside."
        " Delete the album again, then empty Trash."
        " Removed 1, 1 could not be removed ('YYY-stuck')."
        " Also refused: 'ZZZ-inbox' is the inbox (same inode as MUSICDROP_INBOX_DIR)"
        " — move that entry out of Trash."
    )
    assert listed.is_dir(), "the album's only copy is still there"
    assert (trash / "ZZZ-inbox").is_dir()
    assert stuck.is_dir()
    assert not (trash / "MMM-ordinary").exists(), "and the rest was still emptied"


def test_open_checked_dir_refuses_a_trash_it_could_not_stat(tmp_path: Path) -> None:
    """No identity to compare means no compare, so the open is refused instead.

    ``protected.trash`` is ``None`` whenever the Trash was not there when the
    request checked it. Skipping the comparison left ``O_NOFOLLOW`` as the only
    guard, and a rename of the music library onto that path inside the window
    was measured to pass it and rmtree every artist folder.
    """
    trash = tmp_path / "trash"
    trees = _trees_with_trash(trash)
    assert trees.trash is None, "the fixture must build the set before the Trash exists"
    trash.mkdir()
    (trash / "Album").mkdir()

    with pytest.raises(ProtectedTreeError, match="could not be examined when MusicDrop checked"):
        open_checked_dir(trash, trees)
    assert (trash / "Album").is_dir()


def test_empty_all_refuses_a_trash_that_is_one_of_the_apps_own_directories(
    tmp_path: Path,
) -> None:
    """The one tree the remover does not walk: the Trash ROOT itself.

    ``empty_all`` compared every ENTRY against the set and the root against
    nothing, so a bind mount aliasing the Trash onto the music library or the
    beets dir emptied them — measured, ``removed=2`` with the music tree left
    empty. The set already knows: first writer wins, so the Trash's own inode is
    named as the music library when the two are one directory. Here the two
    settings simply point at the same path, which is the same state without a
    mount namespace.
    """
    trash = tmp_path / "trash"
    (trash / "Artist").mkdir(parents=True)
    (trash / "Artist" / "01.flac").write_bytes(b"x")
    trees = protected_trees(
        settings=Settings(),
        music_dir=trash,
        beets_dir=tmp_path / "absent-beets",
        trash_dir=trash,
        origins_dir=tmp_path / "absent-origins",
        library_path=tmp_path / "absent" / "library.db",
    )

    origins = origins_for(trash)
    lib = library_with_no_rows(tmp_path)
    with pytest.raises(ProtectedTreeError, match="the Trash directory is the music library"):
        empty_all(trash, origins_dir=origins, protected=trees, lib=lib)
    assert (trash / "Artist" / "01.flac").exists()


@pytest.mark.parametrize(
    ("setting", "phrase"),
    [
        ("origins", "is the Trash origin store"),
        ("inbox_dir", "is the inbox"),
        ("cover_thumb_cache_dir", "is the cover-thumbnail cache"),
    ],
)
def test_the_trash_root_alias_is_found_wherever_the_twin_is_listed(
    tmp_path: Path, setting: str, phrase: str
) -> None:
    """The alias check does not depend on where the twin sits in the list.

    ``protected_entries`` names the Trash third and ``ids`` keeps one owner per
    inode, so reading ``ids[trash]`` back answered "the Trash directory" for
    every participant listed after it and the root alias went unseen. Measured:
    the Trash spelled as the origin store, the inbox or either cache OPENED.
    """
    trash = tmp_path / "trash"
    (trash / "Artist").mkdir(parents=True)
    (trash / "Artist" / "01.flac").write_bytes(b"x")

    def spelled(name: str, absent: str) -> str:
        return str(trash if setting == name else tmp_path / absent)

    trees = protected_trees(
        settings=Settings(
            inbox_dir=spelled("inbox_dir", "absent-inbox"),
            cover_thumb_cache_dir=spelled("cover_thumb_cache_dir", "absent-thumbs"),
        ),
        music_dir=tmp_path / "absent-music",
        beets_dir=tmp_path / "absent-beets",
        trash_dir=trash,
        origins_dir=trash if setting == "origins" else tmp_path / "absent-origins",
        library_path=tmp_path / "absent" / "library.db",
    )

    origins = origins_for(trash)
    lib = library_with_no_rows(tmp_path)
    with pytest.raises(ProtectedTreeError, match=f"the Trash directory {phrase}"):
        empty_all(trash, origins_dir=origins, protected=trees, lib=lib)
    assert (trash / "Artist" / "01.flac").exists()


def test_a_protected_directory_the_walk_cannot_open_is_still_refused(tmp_path: Path) -> None:
    """``stat`` needs the parent's ``x`` bit; ``opendir`` needs the ``r`` bit.

    Comparing each directory when the walk REACHED it meant a protected store at
    mode 000 was never compared at all — measured, the album delete moved the
    inbox into Trash. Every directory is stat'd from its parent's descriptor
    instead, so the read bit decides only what is found BELOW it.

    The control is the same tree readable, which must still refuse: this is the
    permission bit ceasing to matter, not the guard firing on something else.
    """
    if os.getuid() == 0:
        pytest.skip("root reads a mode-000 directory anyway")
    entry = tmp_path / "entry"
    store = entry / "inbox"
    store.mkdir(parents=True)
    trees = protected_trees(
        settings=Settings(inbox_dir=str(store)),
        music_dir=tmp_path / "absent-music",
        beets_dir=tmp_path / "absent-beets",
        trash_dir=tmp_path / "absent-trash",
        origins_dir=tmp_path / "absent-origins",
        library_path=tmp_path / "absent" / "library.db",
    )
    assert protected_match(entry, trees) == "contains the inbox (same inode as MUSICDROP_INBOX_DIR)"

    store.chmod(0o000)
    try:
        assert protected_match(entry, trees) == (
            "contains the inbox (same inode as MUSICDROP_INBOX_DIR)"
        )
    finally:
        store.chmod(0o755)


def test_the_refusal_names_how_many_entries_could_not_be_removed(tmp_path: Path) -> None:
    """The refused raise outranks the partial, so it has to carry both counts.

    Without the failed count an entry the app could not remove was reported
    nowhere and stayed invisible on every retry, while the sentence told the user
    the layout was the only thing wrong.
    """
    if os.getuid() == 0:
        pytest.skip("root removes a mode-500 directory's children anyway")
    trash = tmp_path / "trash"
    (trash / "Ordinary").mkdir(parents=True)
    (trash / "Stuck" / "child").mkdir(parents=True)
    music = tmp_path / "music"
    music.mkdir()
    os.rename(music, trash / "Sneak")
    origins = origins_for(trash)
    trees = _trees_for(trash / "Sneak", trash)
    (trash / "Stuck").chmod(0o500)
    lib = library_with_no_rows(tmp_path)
    try:
        with pytest.raises(ProtectedTreeError) as caught:
            empty_all(trash, origins_dir=origins, protected=trees, lib=lib)
    finally:
        (trash / "Stuck").chmod(0o700)

    assert "'Sneak' is the music library" in str(caught.value)
    assert "Removed 1, 1 could not be removed" in str(caught.value)


def test_empty_one_refuses_a_protected_entry(tmp_path: Path) -> None:
    """The per-entry route, with its own control on an ordinary entry."""
    trash = tmp_path / "trash"
    trash.mkdir()
    music = tmp_path / "music"
    music.mkdir()
    (music / "01.flac").write_bytes(b"x")
    os.rename(music, trash / "Sneak")
    (trash / "Ordinary").mkdir()
    trees = _trees_for(trash / "Sneak", trash)
    origins = origins_for(trash)
    sneak = str(trash / "Sneak")
    lib = library_with_no_rows(tmp_path)

    with pytest.raises(ProtectedTreeError, match="'Sneak' is the music library"):
        empty_one(sneak, origins_dir=origins, protected=trees, lib=lib)
    assert (trash / "Sneak" / "01.flac").exists()

    assert (
        empty_one(
            str(trash / "Ordinary"),
            origins_dir=origins,
            protected=trees,
            lib=library_with_no_rows(tmp_path),
        ).removed
        == 1
    )


def _rename_onto_the_entry_after_the_guard(
    monkeypatch: pytest.MonkeyPatch, *, entry: Path, impostor: Path, away: Path
) -> list[bool]:
    """Win the guard-to-removal window deterministically. Returns the "it fired" flag.

    The seam is the guard itself: the moment it answers, the entry's NAME is made
    to mean something else. Measured on this branch, the window is 3 µs for a
    52-directory entry — small, and it was won on the first attempt with a rename
    loop, in both delete paths.
    """
    import app.beets.trash_manage as manage

    real = protected_match
    fired: list[bool] = []

    def racing(
        root: str | Path, protected: ProtectedTrees, *, dir_fd: int | None = None
    ) -> str | None:
        clause = real(root, protected, dir_fd=dir_fd)
        if not fired:
            fired.append(True)
            os.rename(entry, away)
            os.rename(impostor, entry)
        return clause

    monkeypatch.setattr(manage, "protected_match", racing)
    return fired


def test_empty_all_removes_the_entry_it_guarded_and_not_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name is one identity at the guard and another at the ``rmtree``.

    Measured before this: with ``<M>`` renamed onto the entry's name inside that
    window, ``empty_all`` answered ``removed=1`` and the music library's
    ``01.flac`` was gone — the Trash ROOT was pinned by ``open_checked_dir``, the
    entry name was not. The entry is opened once and the removal runs through
    THAT descriptor, so the swap costs the entry its ``rmdir`` and nothing else.
    """
    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    (trash / "Album" / "art.jpg").write_bytes(b"x")
    music = tmp_path / "music"
    music.mkdir()
    (music / "01.flac").write_bytes(b"x")
    trees = _trees_for(music, trash)
    fired = _rename_onto_the_entry_after_the_guard(
        monkeypatch, entry=trash / "Album", impostor=music, away=tmp_path / "away"
    )

    origins = origins_for(trash)
    lib = library_with_no_rows(tmp_path)
    with pytest.raises(
        ProtectedTreeError, match="changed between the check and the removal"
    ) as err:
        empty_all(trash, origins_dir=origins, protected=trees, lib=lib)

    assert fired == [True]
    assert (trash / "Album" / "01.flac").exists(), "the music library, under the entry's name"
    # The swap lands AFTER the children are gone, so the summary's "Removed 0"
    # is about an entry that was emptied. Literal, not the constant: sharing it
    # with the source would compare the wording to itself.
    assert "some of its contents were removed" in str(err.value)


def test_empty_one_removes_the_entry_it_guarded_and_not_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same window, in the per-entry route. Same measurement, same answer."""
    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    (trash / "Album" / "art.jpg").write_bytes(b"x")
    music = tmp_path / "music"
    music.mkdir()
    (music / "01.flac").write_bytes(b"x")
    trees = _trees_for(music, trash)
    fired = _rename_onto_the_entry_after_the_guard(
        monkeypatch, entry=trash / "Album", impostor=music, away=tmp_path / "away"
    )

    origins = origins_for(trash)
    entry = str(trash / "Album")
    lib = library_with_no_rows(tmp_path)
    with pytest.raises(
        ProtectedTreeError, match="changed between the check and the removal"
    ) as err:
        empty_one(entry, origins_dir=origins, protected=trees, lib=lib)

    assert fired == [True]
    assert (trash / "Album" / "01.flac").exists(), "the music library, under the entry's name"
    message = str(err.value)
    assert "some of its contents were removed" in message
    assert "Nothing was removed" not in message, "art.jpg was"


def test_an_entry_swapped_between_the_stat_and_the_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window BEFORE the walk, which the two guard-seam tests cannot reach.

    ``_remove_checked_entry`` takes the entry's ``lstat`` and then opens the
    name; between those two syscalls the name can come to mean something else,
    and the walk that would notice has not run yet. The ``fstat`` compare is the
    only thing standing there, so the seam is the open itself.
    """
    import app.beets.trash_manage as manage

    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    (trash / "Album" / "art.jpg").write_bytes(b"x")
    music = tmp_path / "music"
    music.mkdir()
    (music / "01.flac").write_bytes(b"x")
    away = tmp_path / "away"
    real = manage._open_dir
    fired: list[bool] = []

    def racing(name: str, dir_fd: int) -> int:
        if not fired:
            fired.append(True)
            os.rename(trash / "Album", away)
            os.rename(music, trash / "Album")
        return real(name, dir_fd)

    monkeypatch.setattr(manage, "_open_dir", racing)
    trees = _trees_for(music, trash)
    origins = origins_for(trash)
    entry = str(trash / "Album")
    lib = library_with_no_rows(tmp_path)

    with pytest.raises(
        ProtectedTreeError, match="changed between the check and the removal"
    ) as err:
        empty_one(entry, origins_dir=origins, protected=trees, lib=lib)

    assert fired == [True]
    assert (trash / "Album" / "01.flac").exists(), "the music library, under the entry's name"
    assert (away / "art.jpg").exists(), "the real entry, untouched"
    assert "Nothing was removed" in str(err.value)


def _plant_inside_the_entry_after_the_guard(
    monkeypatch: pytest.MonkeyPatch, *, planted: Path, impostor: Path
) -> list[bool]:
    """Move ``impostor`` INTO the entry the moment the guard's walk has answered.

    The other window: the entry's own identity is pinned, so the attack that is
    left is arriving under it. The seam is the walk itself, so the plant lands
    after the last thing that looked at the whole tree and before the first
    child is removed — deterministically, where the real race is 84.8 ms on a
    4 000-subdir entry.
    """
    import app.beets.trash_manage as manage

    real = protected_match
    fired: list[bool] = []

    def racing(
        root: str | Path, protected: ProtectedTrees, *, dir_fd: int | None = None
    ) -> str | None:
        clause = real(root, protected, dir_fd=dir_fd)
        if not fired:
            fired.append(True)
            os.rename(impostor, planted)
        return clause

    monkeypatch.setattr(manage, "protected_match", racing)
    return fired


@pytest.mark.parametrize("path", ["all", "one"])
def test_a_library_moved_in_after_the_walk_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The guard's walk and the removal used to be two traversals.

    Measured before this: with ``<M>`` renamed to ``<trash>/Album/planted``
    after the walk's first yield, both delete paths answered ``removed=1`` and
    the library's ``01.flac`` was gone -- the walk had already passed, and
    ``shutil.rmtree`` enumerated the entry a second time. The identity question
    is now asked at every directory the removal descends into, so a tree that
    arrives mid-removal is compared whenever it arrives.

    ``art.jpg`` sorts before ``planted``: the refusal comes AFTER a real
    removal, which is what the partial clause is about.
    """
    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    (trash / "Album" / "art.jpg").write_bytes(b"x")
    music = tmp_path / "music"
    music.mkdir()
    (music / "01.flac").write_bytes(b"x")
    trees = _trees_for(music, trash)
    fired = _plant_inside_the_entry_after_the_guard(
        monkeypatch, planted=trash / "Album" / "planted", impostor=music
    )
    origins = origins_for(trash)
    # Built before the block, and inert until called: the seam is already armed.
    lib = library_with_no_rows(tmp_path)
    empty = (
        partial(empty_all, trash, origins_dir=origins, protected=trees, lib=lib)
        if path == "all"
        else partial(
            empty_one,
            str(trash / "Album"),
            origins_dir=origins,
            protected=trees,
            lib=lib,
        )
    )

    with pytest.raises(ProtectedTreeError, match="contains the music library") as err:
        empty()

    assert fired == [True]
    assert (trash / "Album" / "planted" / "01.flac").exists(), "the music library"
    assert not (trash / "Album" / "art.jpg").exists(), "the entry's own child DID go"
    assert "some of its contents were removed" in str(err.value)


def test_trash_folder_refuses_a_husk_that_holds_the_inbox(tmp_path: Path) -> None:
    """The orphan sweep's mover, on a shape nothing else stops today.

    The mover is driven directly here, so the route's exclusion list stands
    down: ``_ignore_dirs`` (``api/reorganize.py``) is built from
    ``protected_entries`` and does cover the inbox, but the layout rule has no
    row for an inbox inside the music library. So
    ``MUSICDROP_INBOX_DIR=<music>/Downloads/inbox`` leaves ``Downloads``
    audio-free and reportable, and the mover would take its whole subtree.

    The control is the same husk with the inbox moved out: it IS trashed.
    """
    trash = tmp_path / "trash"
    music = tmp_path / "music"
    husk = music / "Downloads"
    (husk / "inbox").mkdir(parents=True)
    (husk / "poster.jpg").write_bytes(b"x")
    origins = origins_for(trash)
    trees = protected_trees(
        settings=Settings(inbox_dir=str(husk / "inbox")),
        music_dir=music,
        beets_dir=tmp_path / "absent-beets",
        trash_dir=trash,
        origins_dir=origins,
        library_path=tmp_path / "absent" / "library.db",
    )

    with pytest.raises(ProtectedTreeError, match="'Downloads' contains the inbox"):
        trash_folder(husk, trash_dir=trash, origins_dir=origins, protected=trees)
    assert (husk / "poster.jpg").exists()

    (husk / "inbox").rmdir()
    dest = trash_folder(husk, trash_dir=trash, origins_dir=origins, protected=trees)
    assert (dest / "poster.jpg").exists()


def test_delete_does_not_refuse_an_album_folder_that_holds_an_app_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that HOLDS a store has no refusal left to make. MEASURED.

    The refusal existed for the whole-folder mover, which relocated the TREE and
    would have taken the store with it. Owner ruling ``decisions.md`` 58 moves
    the album's own files instead, so the store is never an operand: what this
    pins is that the delete goes through AND the store is untouched — refusing
    here would now leave the operator no way to remove the album, for a directory
    nothing was going to move.

    The inbox stands in for the shape a deployment really reaches this by, which
    is the bind-mount alias in the namespace test below: the same identity
    question, and this one needs no mount namespace.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    folder = music / "Radiohead" / "Kid A"
    folder.mkdir(parents=True)
    lib = build_library(str(tmp_path / "library.db"), str(music))
    _add_album(lib, folder)
    inbox = folder / "inbox"
    inbox.mkdir()
    (inbox / "waiting.mp3").write_bytes(b"\x00")
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))

    trash = tmp_path / "trash"
    origins = origins_for(trash)
    album_id = _require_id(next(iter(lib.albums())).id)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    assert protected_match(folder, trees) is not None, "the folder must hold a protected dir"

    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(lib.albums()) == []  # the album went
    assert not (folder / "01 Track.mp3").exists()  # its file went
    assert (inbox / "waiting.mp3").is_file()  # the store did not, contents and all
    assert not list(trash.glob("**/waiting.mp3"))


def test_restore_refuses_a_trash_entry_that_holds_an_app_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore is the third mover, and it discarded the set the route had built.

    Both arms relocate the whole entry out of Trash — the move-back straight to
    the recorded origin, the fallback through a move-import — so the guard sits
    at the entry point above the branch. The namespace test measures the shape
    that destroyed data; this one needs no mount and covers both arms.

    The control is the same entry with the store moved out: the restore runs.
    """
    from app.beets.trash_manage import restore_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    (music / "Artist").mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    trash = tmp_path / "trash"
    entry = trash / "Some Album"
    (entry / "inbox").mkdir(parents=True)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(entry / "inbox"))
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    entry_path = str(entry)

    with pytest.raises(ProtectedTreeError, match="'Some Album' contains the inbox"):
        restore_album(lib, entry_path, trash_dir=trash, origins_dir=origins, protected=trees)
    assert (entry / "inbox").is_dir()

    (entry / "inbox").rmdir()
    assert (
        restore_album(
            lib, entry_path, trash_dir=trash, origins_dir=origins, protected=trees
        ).restored
        is False
    )


def test_delete_artist_fans_out_past_an_album_folder_that_holds_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fan-out had a pre-loop guard, and this is why it is gone.

    Asked per album inside the loop, an artist whose SECOND album held the store
    answered the partial 500 — "the delete stopped after 1 of 2 albums had been
    moved to Trash (... Nothing was moved.)" — after the first album was already
    in Trash and its rows dropped. The pre-loop check fixed the sentence; owner
    ruling ``decisions.md`` 58 removes the question, because a per-item move
    never takes the store. What is pinned now is that BOTH albums are deleted and
    the store inside the second one keeps its contents.
    """
    from app.beets.delete import delete_artist
    from tests.conftest import build_library

    music = tmp_path / "music"
    first = music / "Twosome" / "A First"
    second = music / "Twosome" / "B Second"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, first)
    _add_album(lib, second)
    inbox = second / "inbox"
    inbox.mkdir()
    (inbox / "waiting.mp3").write_bytes(b"\x00")
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)

    result = delete_artist(lib, "Radiohead", trash_dir=trash, origins_dir=origins, protected=trees)

    assert result.trashed_albums == 2
    assert list(lib.albums()) == []
    assert not (first / "01 Track.mp3").exists()
    assert not (second / "01 Track.mp3").exists()
    assert (inbox / "waiting.mp3").is_file()  # the store is not the delete's operand


def test_delete_artist_still_deletes_on_a_flat_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The album root IS the protected music dir, and the delete still lands.

    The music library is in the protected set, and it is also where
    ``_dirs_the_prune_can_reach`` STOPS, so this is the layout where that walk
    collects nothing at all: the delete must go through anyway rather than refuse
    or leave the root behind.

    The ``music.is_dir()`` assert below is a floor, not a pin on the re-create:
    measured, an unguarded ``prune_dirs`` leaves the library root standing too,
    because ``ancestry`` excludes the path itself and so the root is never
    removed — which is also why collecting nothing here is right rather than a
    hole. What kills an unguarded version is
    ``test_an_album_whose_folder_is_a_store_is_deleted_and_the_store_survives``.
    """
    from app.beets.delete import delete_artist
    from tests.conftest import build_library

    music = tmp_path / "music"
    music.mkdir()
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, music)
    trash = tmp_path / "trash"
    monkeypatch.setattr("app.config.settings.inbox_dir", str(tmp_path / "absent-inbox"))
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins_for(trash))
    assert protected_match(music, trees) is not None, "the music dir must be in the set"

    result = delete_artist(
        lib, "Radiohead", trash_dir=trash, origins_dir=origins_for(trash), protected=trees
    )
    assert result.trashed_albums == 1
    assert list(lib.albums()) == []
    assert music.is_dir(), "the library root must survive its last album"


@pytest.mark.parametrize("route", ["album", "artist"])
def test_the_delete_ops_build_the_set_the_keep_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Both ops, through ``_checked_store``, building a set that is really used.

    Measured without this: handing ``delete_album`` an EMPTY set in the op left
    ``test_delete.py`` and this file green, because every other test here calls
    the adapter directly. The op is where the set is BUILT, so it needs its own
    pin — one per route, since each has its own call.

    What the set decides now is not a refusal but the keep-file: with an empty
    one, ``_keep_our_dirs`` reads the inbox as a stranger's directory, plants
    nothing and beets' prune takes it. So the album's folder IS the inbox here,
    where the old version of this test had the inbox merely inside it.
    """
    import asyncio
    from types import SimpleNamespace

    from app.beets.delete import delete_album_op, delete_artist_op
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    inbox.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, inbox)
    trash = tmp_path / "trash"
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))

    class _App:
        state = SimpleNamespace(
            beets_library=make_test_handle(lib, beets_dir),
            settings=Settings(
                trash_dir=str(trash),
                trash_origins_dir=str(origins_for(trash)),
                inbox_dir=str(inbox),
            ),
        )

    class _Req:
        app = _App()

    album_id = _require_id(next(iter(lib.albums())).id)
    op = (
        delete_album_op(_Req(), album_id)  # type: ignore[arg-type]  # stub req
        if route == "album"
        else delete_artist_op(_Req(), "Radiohead")  # type: ignore[arg-type]  # ditto
    )
    result = asyncio.run(op)

    assert result.trashed_albums == 1
    assert list(lib.albums()) == []
    assert not (inbox / "01 Track.mp3").exists()  # the album's file really moved
    assert inbox.is_dir(), "the op's own set is what puts the store back"
    # ...and the op's check created the Trash ROOT through the anchored walk,
    # which is the other thing this call is here for.
    assert [p.name for p in trash.iterdir()] == ["Radiohead - Kid A"]


# --------------------------------------------------------------------------
# The routes.
# --------------------------------------------------------------------------


def _trash_dir(client: TestClient) -> Path:
    return Path(client.get("/api/trash").json()["trash_path"])


def test_the_empty_routes_refuse_a_store_that_sits_inside_a_trash_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2 through the routes, with nothing patched but a setting.

    ``MUSICDROP_INBOX_DIR`` inside a Trash entry used to be the in-process
    fixture for D1's guard — the layout rule had no row for it, so the walk was
    the only thing that caught it. D2 gives it one, and the layout check runs
    FIRST, so this is now a refusal by the predicate: nothing is walked and
    NOTHING is removed, including the ordinary entry beside it. That last
    assertion is what makes this stronger than the D1 version it replaces, which
    could only report the store after having removed the other entry.
    """
    trash = _trash_dir(client)
    (trash / "Sneak" / "inbox").mkdir(parents=True)
    (trash / "Sneak" / "keep.flac").write_bytes(b"x")
    (trash / "Ordinary").mkdir()
    monkeypatch.setattr("app.config.settings.inbox_dir", str(trash / "Sneak" / "inbox"))

    one = client.delete("/api/trash", params={"folder": "Sneak"})
    assert one.status_code == 503, one.text
    assert "The Trash directory contains the inbox" in one.json()["detail"]

    every = client.delete("/api/trash/all")
    assert every.status_code == 503, every.text
    assert "The Trash directory contains the inbox" in every.json()["detail"]
    assert (trash / "Sneak" / "keep.flac").exists()
    assert (trash / "Ordinary").exists()


def test_the_empty_routes_are_wired_to_the_guard_as_well_as_to_the_predicate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1's guard, reached through both remove routes.

    ``checked_store_dirs`` is stood down for this test — NOT the guard under
    test — because with D2 in place there is no spelling of a protected
    directory inside a Trash entry that the predicate still allows: every one of
    the eleven protected paths has a row against ``T``, measured by walking the
    table. The layout check runs first, so it answers every in-process fixture
    (the test above is that control, unpatched).

    What still reaches the guard in the real world is an ALIAS the predicate
    cannot see — a bind mount — and that is measured against a real one in
    ``test_a_bind_mounted_beets_dir_is_refused_where_the_spelled_rule_allows_it``,
    in a child process, on ``empty_all`` itself. What this test adds is the
    WIRING: that both routes hand ``empty_one``/``empty_all`` a protected set at
    all, which a subprocess probe calling the primitive directly cannot show.

    The ordinary entry beside the refused one is the control: Empty all removes
    it and says so in the same message.
    """
    from app.beets.store_layout import _resolve_store_dirs

    trash = _trash_dir(client)
    (trash / "Sneak" / "inbox").mkdir(parents=True)
    (trash / "Sneak" / "keep.flac").write_bytes(b"x")
    (trash / "Ordinary").mkdir()
    monkeypatch.setattr("app.config.settings.inbox_dir", str(trash / "Sneak" / "inbox"))
    monkeypatch.setattr("app.api.trash.checked_store_dirs", _resolve_store_dirs)

    one = client.delete("/api/trash", params={"folder": "Sneak"})
    assert one.status_code == 503, one.text
    assert "'Sneak' contains the inbox" in one.json()["detail"]

    every = client.delete("/api/trash/all")
    assert every.status_code == 503, every.text
    assert "'Sneak' contains the inbox" in every.json()["detail"]
    assert "Removed 1" in every.json()["detail"]
    assert (trash / "Sneak" / "keep.flac").exists()
    assert not (trash / "Ordinary").exists()


def test_the_orphan_sweep_skips_a_protected_candidate_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused husk costs the run a WARNING, not the whole pass.

    The control is the ordinary husk beside it: it still reaches Trash, so the
    ``continue`` skips ONE candidate rather than ending the loop. The refused one
    is an inbox inside the library, reached here with ``ignore_dirs=()`` so the
    route's own exclusion list — which does cover the inbox — stands down (see
    :func:`test_trash_folder_refuses_a_husk_that_holds_the_inbox`).
    """
    from app.beets.orphans import find_orphan_folders
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import _sweep_orphans
    from tests.conftest import build_library

    music = tmp_path / "music"
    music.mkdir()
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    handle = make_test_handle(lib, beets_dir)
    trash = tmp_path / "trash"
    (music / "Downloads" / "inbox").mkdir(parents=True)
    (music / "Downloads" / "poster.jpg").write_bytes(b"x")
    (music / "Plain").mkdir()
    (music / "Plain" / "poster.jpg").write_bytes(b"x")
    monkeypatch.setattr("app.config.settings.inbox_dir", str(music / "Downloads" / "inbox"))
    assert set(find_orphan_folders(music, seeds=None, trash_dir=trash)) == {
        music / "Downloads",
        music / "Plain",
    }

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    with caplog.at_level(logging.WARNING, logger="app.reorganize_jobs.runner"):
        _sweep_orphans(
            reg,
            handle,
            scope="library",
            music_dir=music,
            trash_dir=trash,
            trash_origins_dir=origins_for(trash),
            vacated=[],
            ignore_dirs=(),
            protected_dirs=(),
        )

    assert "orphan sweep skipped a folder" in caplog.text
    assert (music / "Downloads" / "poster.jpg").exists()
    assert not (music / "Plain").exists()


def test_the_orphan_sweep_reads_the_settings_the_route_threaded_in(tmp_path: Path) -> None:
    """The route's instance decides the protected set, not the module global.

    The test above configures the inbox by patching ``app.config.settings``, so
    it passes either way. Here nothing is patched: the inbox is set only on the
    ``Settings`` handed to the sweep. ``app.state.settings`` is what the route
    reads to build its ignore list, and this phase runs on another thread minutes
    later — reading the import-bound global here let the two name different
    objects.

    The control is the husk beside it, which still reaches Trash.
    """
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from app.reorganize_jobs.runner import _sweep_orphans
    from tests.conftest import build_library

    music = tmp_path / "music"
    music.mkdir()
    beets_dir = beets_dir_for(tmp_path)
    handle = make_test_handle(build_library(str(beets_dir / "library.db"), str(music)), beets_dir)
    trash = tmp_path / "trash"
    inbox = music / "Downloads" / "inbox"
    inbox.mkdir(parents=True)
    (music / "Downloads" / "poster.jpg").write_bytes(b"x")
    (music / "Plain").mkdir()
    (music / "Plain" / "poster.jpg").write_bytes(b"x")

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    _sweep_orphans(
        reg,
        handle,
        scope="library",
        music_dir=music,
        trash_dir=trash,
        trash_origins_dir=origins_for(trash),
        vacated=[],
        ignore_dirs=(),
        protected_dirs=(),
        settings=Settings(inbox_dir=str(inbox)),
    )

    assert (music / "Downloads" / "poster.jpg").exists()
    assert not (music / "Plain").exists()


def test_the_sweep_builds_its_set_from_the_pair_the_layout_check_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arguments, asserted directly rather than through an outcome.

    The test above only reaches ``settings=``: it turns on a CONFIGURED inbox,
    so the path arguments could each be wrong and it would still refuse. They
    are what tie the set to the same music root, beets dir and pair
    ``check_store_layout`` has just accepted, which is the whole point of taking
    it beside that call.

    Four of the six moved INSIDE the callee: the sweep asks
    ``checked_protected_trees``, which derives the music root, the beets dir and
    the database path from this handle itself, so they cannot drift from the pair
    — what is left to pin here is the handle, the settings object the route
    threaded in, and the two paths this phase was given.
    """
    from app.reorganize_jobs import runner as runner_mod
    from app.reorganize_jobs.registry import ReorganizeRegistry
    from tests.conftest import build_library

    music = tmp_path / "music"
    music.mkdir()
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    handle = make_test_handle(lib, beets_dir)
    trash = tmp_path / "trash"
    seen: dict[str, object] = {}

    def spy(
        settings_arg: object, handle_arg: object, *, trash_dir: Path, origins_dir: Path
    ) -> ProtectedTrees:
        seen.update(
            settings=settings_arg, handle=handle_arg, trash_dir=trash_dir, origins_dir=origins_dir
        )
        return ProtectedTrees(ids={}, trash=None, trash_alias=None, trash_spellings=(trash_dir,))

    monkeypatch.setattr(runner_mod, "checked_protected_trees", spy)

    reg = ReorganizeRegistry()
    reg.start(scope="library", artist=None, album_id=None, scope_label="library")
    runner_mod._sweep_orphans(
        reg,
        handle,
        scope="library",
        music_dir=music,
        trash_dir=trash,
        trash_origins_dir=origins_for(trash),
        vacated=[],
        ignore_dirs=(),
        protected_dirs=(),
    )

    assert seen["settings"] is settings
    assert seen["handle"] is handle
    assert seen["trash_dir"] == trash
    assert seen["origins_dir"] == origins_for(trash)


# --------------------------------------------------------------------------
# The alias no spelled rule can see.
# --------------------------------------------------------------------------


@pytest.mark.skipif(not unshare_works(), reason="no unprivileged mount namespace on this box")
def test_a_bind_mounted_beets_dir_is_refused_where_the_spelled_rule_allows_it(
    tmp_path: Path,
) -> None:
    """D1's invariant, and the control that shows why the guard is not redundant.

    One child process, four measurements: ``check_store_layout`` ALLOWS
    ``-v /srv/music/musicdrop:/data/beets`` (a mount point's ancestors are its
    own, so no "contains" row can reach the source); the mover's guard refuses
    it; ``empty_all`` with an EMPTY id set — this tree before the guard — deletes
    ``library.db`` through a Trash entry aliased the same way; and with the real
    set it refuses and the database survives.
    """
    work = tmp_path / "work"
    work.mkdir()
    lines = run_probe("bind_beets_dir", work)

    assert "spelled-rule ALLOWED" in lines, lines
    assert "mover REFUSED" in lines, lines
    assert "control-empty-all TrashEmptyPartialError" in lines, lines
    assert "control-lost-the-db True" in lines, lines
    assert "guarded-empty-all REFUSED" in lines, lines
    assert "db-survives True" in lines, lines


@pytest.mark.skipif(not unshare_works(), reason="no unprivileged mount namespace on this box")
def test_restore_refuses_a_trash_entry_that_is_the_music_library(tmp_path: Path) -> None:
    """Restore is a mover, and this is the shape where it deleted the library.

    ``-v <host>/data:/data`` beside ``-v <host>/data/trash/music:/music`` gives
    the Trash an entry that IS the music library by inode while every spelled row
    allows the layout. A stale ``music`` origin record then sent the move-back
    across the two mounts: EXDEV, ``copytree`` into a destination inside its own
    source, ``rmtree`` of the source.

    Two independent refusals, so either alone holds: the request's protected set
    reaching ``restore_album``, and — with that set emptied — ``move_no_merge``
    answering EINVAL (22) because the destination's ancestry reaches the source
    by identity, which is what ``os.rename`` answers for the same shape on one
    filesystem.
    """
    work = tmp_path / "work"
    work.mkdir()
    lines = run_probe("bind_trash_holds_music", work)

    assert "spelled-rule ALLOWED" in lines, lines
    assert "guarded-restore REFUSED" in lines, lines
    assert "guarded-track-survives True" in lines, lines
    assert "bare-move REFUSED 22" in lines, lines
    assert "bare-track-survives True" in lines, lines
    assert "bare-origin-absent True" in lines, lines
    assert "unguarded-restore TrashRestoreIncompleteError" in lines, lines
    assert "unguarded-track-survives True" in lines, lines


def test_the_empty_set_refuses_nothing(tmp_path: Path) -> None:
    """The control for every assertion above: with no identities, nothing matches."""
    music = tmp_path / "music"
    music.mkdir()
    empty = ProtectedTrees(ids={}, trash=None, trash_alias=None, trash_spellings=())
    assert protected_match(music, empty) is None
    refuse_protected_tree(music, empty, action="moved")


def test_an_album_whose_folder_is_a_store_is_deleted_and_the_store_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An album imported in place into the store ROOT still has a delete.

    Measured on the parent commit: it answered 503 "\'inbox\' is the inbox",
    which leaves the operator no way to remove the album at all — while an album
    SHARING that folder with another took the per-item mover and was removed.
    The two shapes differ only in whether a second album is filed beside it.

    The second half is why the fall-through is not just "drop the guard": beets\'
    ``prune_dirs`` rmtree\'s every emptied ancestor up to the music root, and
    measured on ``trash_album`` as it stands, that removed the inbox AND the
    folder above it (``inbox still a dir: False | Downloads still a dir:
    False``). The re-create used to live inside the whole-folder mover and moved
    to ``delete._trash_one`` with owner ruling ``decisions.md`` 58, so this now
    drives ``delete_album`` — the per-item primitive itself still has the hole,
    which duplicates resolve and import Replace have always had (a recorded
    residual: they hold no ``ProtectedTrees``).

    The single-disc, store-IS-the-folder shape only. The two below are the ones
    it does not reach: a multi-disc album, where the store is the COMMONPATH and
    not any item's own directory, and a store ABOVE the album folder.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    inbox.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, inbox)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    assert protected_match(inbox, trees) is not None, "the inbox must be in the set"

    album_id = _require_id(next(iter(lib.albums())).id)
    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(lib.albums()) == []
    assert inbox.is_dir(), "the store the guard protects must still be there"
    assert (music / "Downloads").is_dir(), "and the folder above it, which prune took too"
    assert not (inbox / "01 Track.mp3").exists()


def test_a_multi_disc_album_whose_root_is_a_store_leaves_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is the album's root, and NO item's own directory. MEASURED.

    ``CD1``/``CD2`` directly under the inbox: the first item's folder is
    ``<inbox>/CD1``, so a guard asking about that one answers "not ours" and
    beets' prune climbs through the inbox and removes it — measured on the
    parent of this fix, ``store still a dir: False`` against ``True`` for the
    whole-folder mover it replaced.

    ``_dirs_the_prune_can_reach`` asks per ITEM and climbs, which covers the
    disc levels and the commonpath in one walk without having to compute either.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    (inbox / "CD1").mkdir(parents=True)
    (inbox / "CD2").mkdir()
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_multi_disc_album(lib, inbox)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    assert protected_match(inbox, trees) is not None, "the inbox must be in the set"
    assert not any(protected_match(inbox / d, trees) for d in ("CD1", "CD2")), (
        "no item's OWN folder is the store — that is the whole shape"
    )

    album_id = _require_id(next(iter(lib.albums())).id)
    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(lib.albums()) == []
    assert not list(inbox.rglob("*.mp3")), "both discs' files moved"
    assert len(list(trash.rglob("*.mp3"))) == 2
    assert inbox.is_dir(), "the store the album sat in must still be there"


def test_a_store_above_the_album_folder_survives_the_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is an ANCESTOR, which no question about the album folder reaches.

    An album imported in place at ``<inbox>/Art/Alb``. beets prunes inside
    ``Album.move`` and climbs to ``lib.directory``, so emptying the album folder
    takes ``Art`` and then the inbox itself — measured, ``store dir exists:
    False`` with the app's own prune monkeypatched OUT, which is what says the
    climb is beets' and not ours to delete.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)

    album_id = _require_id(next(iter(lib.albums())).id)
    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(lib.albums()) == []
    assert not folder.exists(), "the album's own folder is beets' to prune"
    assert inbox.is_dir(), "the store above it is not"


def test_a_store_is_still_there_when_the_delete_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prune happens INSIDE ``Album.move``, so a later raise is too late.

    ``Album.remove`` raising is the real shape (beets sends ``album_removed`` to
    plugins with no try/except), and by then the files are in Trash and the
    prune has already run. The keep-file is what the inbox still being here
    proves: it was never removed, so there is nothing to put back.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    inbox.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, inbox)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)

    album = next(iter(lib.albums()))
    album_id = _require_id(album.id)

    def _raises(self: Any, delete: bool = False, with_items: bool = True) -> None:
        raise RuntimeError("a plugin listener said no")

    monkeypatch.setattr(type(album), "remove", _raises)

    with pytest.raises(RuntimeError, match="a plugin listener said no"):
        delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(trash.rglob("*.mp3")), "the files reached Trash before the raise"
    assert inbox.is_dir(), "and the store was never taken"


def test_a_delete_that_refuses_leaves_the_mountpoint_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No path of a Delete CREATES a directory under the library. MEASURED.

    The share dropping mid-move leaves the local mountpoint present and empty,
    which is the one state ``require_library_root`` reads as "not mounted". A
    delete that put its store back in a ``finally`` built that directory on the
    bare mountpoint, the guard then PASSED, and Disk Sync offered to drop 3 of 3
    rows — the mechanism ``store_layout``'s own comment records as security-seat
    H-1. Prevention has no such exit: a keep-file is never created where the
    directory is not already there.
    """
    from app.beets.delete import delete_album
    from app.beets.library import LibraryRootUnavailableError, require_library_root
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    album_id = _require_id(next(iter(lib.albums())).id)

    def _the_share_goes(*args: Any, **kwargs: Any) -> str:
        shutil.rmtree(music)
        music.mkdir()  # the local mountpoint, present and empty
        raise LibraryRootUnavailableError("Library folder is empty. Is the music share mounted?")

    # The name DELETE holds: it imports the primitive, so patching the defining
    # module would leave the real one running.
    monkeypatch.setattr("app.beets.delete.trash_album", _the_share_goes)

    with pytest.raises(LibraryRootUnavailableError):
        delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(music.iterdir()) == [], "nothing was planted on the dead mountpoint"
    with pytest.raises(LibraryRootUnavailableError):
        require_library_root(lib)


def test_a_store_keeps_its_inode_mode_and_contents_through_a_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is the SAME directory afterwards, not a new one with its name.

    ``prune_dirs`` rmtree's an ancestor that is empty OR CLUTTER-ONLY, contents
    and all — under beets' own default ``['Thumbs.DB', '.DS_Store']``, so a store
    holding one ``.DS_Store`` was destroyed and a re-create put back an empty
    shell with a new inode and umask mode (0o700 -> 0o755, measured). A keep-file
    makes the prune break instead, so all three are trivially preserved.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    # Clutter by beets' own default list, so the store is "empty enough" to
    # rmtree without any config change.
    (inbox / ".DS_Store").write_bytes(b"\x00clutter")
    inbox.chmod(0o700)
    before = inbox.stat()

    album_id = _require_id(next(iter(lib.albums())).id)
    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    after = inbox.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), "the SAME directory"
    assert after.st_mode == before.st_mode, "0o700 stays 0o700"
    assert sorted(p.name for p in inbox.iterdir()) == [".DS_Store"], (
        "its contents survived, and the keep-file was removed"
    )


def test_a_store_holding_one_of_two_item_folders_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk is per ITEM, and two items can have no chain in common.

    Item 1 in ``music/B`` and item 2 directly in the store: the album root is the
    music dir itself, so a walk from the root collects nothing and the store is
    pruned. Measured — asking about ``items[:1]`` only left the whole suite green
    while this shape lost the inbox.
    """
    from beets.library import Item

    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    elsewhere = music / "B"
    inbox = music / "inbox"
    elsewhere.mkdir(parents=True)
    inbox.mkdir()
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    items = []
    for i, at in enumerate((elsewhere, inbox), 1):
        track = at / f"{i:02d} Track.mp3"
        track.write_bytes(b"\x00")
        item = Item(album="Kid A", albumartist="Radiohead", artist="Radiohead", track=i)
        item.path = os.fsencode(str(track))
        items.append(item)
    lib.add_album(items)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)

    album_id = _require_id(next(iter(lib.albums())).id)
    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(lib.albums()) == []
    assert len(list(trash.rglob("*.mp3"))) == 2, "both items moved"
    assert inbox.is_dir(), "the store the SECOND item sat in must still be there"
    assert not elsewhere.exists(), "the other item's own folder is beets' to prune"


def test_a_keep_file_that_cannot_be_removed_does_not_fail_the_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planting and removing the keep-file are tidy-ups, and may not fail a delete.

    By the time the ``finally`` runs the files are in Trash and the rows are
    gone, so an unlink that cannot happen has nothing left to protect — raising
    would turn a delete that happened into a 500 saying it did not. The leftover
    keep-file is what the next delete finds and leaves alone.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    inbox.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, inbox)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)

    real_unlink = os.unlink

    def _refuses(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path) == ".musicdrop-keep":
            raise PermissionError(13, "Permission denied")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("app.beets.delete.os.unlink", _refuses)

    album_id = _require_id(next(iter(lib.albums())).id)
    result = delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert result.trashed_albums == 1
    assert list(lib.albums()) == []
    assert list(trash.rglob("*.mp3")), "the delete really happened"
    assert inbox.is_dir(), "the store is there, keep-file and all"
    assert (inbox / ".musicdrop-keep").is_file(), "left behind, which the next delete adopts"


def test_a_store_holding_only_the_cover_survives_the_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Album.move_art`` prunes from the COVER's directory, which is a second chain.

    beets 2.13.1 ``library/models.py:446``: ``move_art`` runs its own
    ``prune_dirs(dirname(old_art), directory, clutter=…)``. Measured with the
    walk asking about item directories only — the store holding the cover was
    rmtree'd, contents and all, by a delete whose tracks live elsewhere.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    folder = music / "Art" / "Alb"
    folder.mkdir(parents=True)
    art_store = music / "Downloads" / "inbox"
    art_store.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    cover = art_store / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xffcover")
    album = next(iter(lib.albums()))
    album.artpath = os.fsencode(str(cover))
    album.store()
    monkeypatch.setattr("app.config.settings.inbox_dir", str(art_store))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    before = art_store.stat()

    delete_album(lib, _require_id(album.id), trash_dir=trash, origins_dir=origins, protected=trees)

    after = art_store.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), "the SAME directory"
    assert not cover.exists(), "the cover itself travelled, as beets moves it"


def test_a_delete_that_raises_while_collecting_leaks_no_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acquire, then more code, then ``try`` — measured as one leaked fd per attempt.

    ``_keep_our_dirs`` reads the user's ``clutter:`` list after the descriptors
    are open and the keep-files planted; a broken value (``clutter: 5``) raises
    there. Above the caller's ``try`` that left the descriptor open and the
    keep-file planted on every attempt.
    """
    import beets

    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    album_id = _require_id(next(iter(lib.albums())).id)
    was = beets.config["clutter"].get()
    beets.config["clutter"] = 5  # not a string and not a list
    open_before = _open_descriptors()

    try:
        with pytest.raises(Exception, match="clutter"):
            delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)
    finally:
        beets.config["clutter"] = was

    assert _open_descriptors() == open_before, "the descriptors it had opened were closed"
    assert not (inbox / ".musicdrop-keep").exists(), "and the keep-file it had planted went"


def _open_descriptors() -> Counter[str]:
    """This process's open descriptors, by WHAT each points at.

    A set of NUMBERS DETECTS a leak just as well — measured both ways, and the
    numbers-only version of this helper kills the same mutants. What it cannot do
    is say what leaked: ``listdir`` opens a descriptor of its own, the leak takes
    the number that one had, and the next sample's listdir takes the number after
    it — so the set difference for a leak on fd 3 is ``{'4'}``, a descriptor that
    was never the leak (measured). Targets name the file. The listdir's own
    descriptor is closed by the time its number is read back, so it is absent
    from both samples rather than counted in one.
    """
    targets: Counter[str] = Counter()
    for fd in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            targets[os.readlink(f"/proc/self/fd/{fd}")] += 1
    return targets


def test_a_plant_that_raises_leaks_no_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor is recorded BEFORE the keep-file work runs on it.

    ``_Kept(path, fd, _plant(path, fd))`` evaluated ``_plant`` first, so its
    ``os.fstat`` raising the module's own failure class (EIO/ESTALE) lost the
    descriptor before anything held it — measured, 1 leaked fd per attempt.

    The keep-file itself is LEFT, and that is the same state a killed run leaves:
    ``_plant`` could not identify the file it had just created, so releasing may
    not remove it, and the next delete adopts it
    (``test_a_leftover_keep_file_is_adopted_and_left_where_it_is``).
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    album_id = _require_id(next(iter(lib.albums())).id)

    real_fstat = os.fstat

    def _eio(fd: int) -> os.stat_result:
        # Only for the keep-FILE's descriptor: ``os`` is one module object, so a
        # blanket fake also breaks the directory fstat that decides whether the
        # directory is ours, and then nothing is kept and nothing plants.
        st = real_fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            return st
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr("app.beets.delete.os.fstat", _eio)
    open_before = _open_descriptors()

    with pytest.raises(OSError, match="Input/output error"):
        delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    monkeypatch.undo()
    assert _open_descriptors() == open_before, "the descriptor it had opened was closed"
    assert (inbox / ".musicdrop-keep").is_file(), "left the way a killed run leaves one"


def test_a_delete_closes_every_descriptor_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One per app-owned directory walked, on the ORDINARY path.

    Measured: deleting either ``os.close`` — the not-ours arm in
    ``open_if_one_of_ours`` or the one in ``_release_our_dirs`` — leaves the
    whole suite green while every delete leaks a descriptor per directory it
    walked, until the server stops at EMFILE.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    album_id = _require_id(next(iter(lib.albums())).id)
    open_before = _open_descriptors()

    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert _open_descriptors() == open_before


def test_a_file_that_replaces_the_keep_file_is_not_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release unlinks by NAME, so it asks whether the name is still ours.

    Measured before the identity check: a file renamed onto ``.musicdrop-keep``
    inside the store after the move was destroyed by the ``finally``. The racer
    is another process on the host — MusicDrop's own destructive routes are
    serialized — but the cost was somebody else's file.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    album_id = _require_id(next(iter(lib.albums())).id)
    precious = inbox / "precious.bin"
    precious.write_bytes(b"someone else's")
    real_carry = delete_mod._carry_the_sidecars

    def _swaps(*args: Any, **kwargs: Any) -> None:
        # After the move, before the ``finally``: the window a racer has.
        os.replace(precious, inbox / ".musicdrop-keep")
        real_carry(*args, **kwargs)

    monkeypatch.setattr(delete_mod, "_carry_the_sidecars", _swaps)

    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert (inbox / ".musicdrop-keep").read_bytes() == b"someone else's", "not ours, not removed"


def test_a_link_that_replaces_the_keep_file_is_not_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``follow_symlinks=False``: the identity is the NAME's, not its target's.

    The one racer shape the test above cannot make. A link left at the name and
    pointing at a hardlink of the keep-file answers the planted identity through
    ``stat`` — measured, following the link removed a link the app never created
    while its own file stayed. ``lstat`` sees the link's own inode and leaves it.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    album_id = _require_id(next(iter(lib.albums())).id)
    real_carry = delete_mod._carry_the_sidecars
    keep = inbox / ".musicdrop-keep"
    twin = inbox / "keep-twin"

    def _relinks(*args: Any, **kwargs: Any) -> None:
        # After the move, before the ``finally``. The twin shares the keep-file's
        # inode, so following the link lands on exactly the planted identity.
        os.link(keep, twin)
        keep.unlink()
        keep.symlink_to(twin.name)
        real_carry(*args, **kwargs)

    monkeypatch.setattr(delete_mod, "_carry_the_sidecars", _relinks)

    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert keep.is_symlink(), "a link at the name is not the file this delete planted"
    assert twin.is_file(), "and its target is untouched"


def test_no_keep_file_is_planted_in_the_music_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk stops at ``lib.directory``, which is where beets' prune stops.

    The music root is one of the app's own directories, so without that stop a
    keep-file would be planted in it on EVERY delete and the user would find a
    dot-file in the top of their library. beets never prunes the root, so there
    is nothing to hold off there.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    folder = music / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    album_id = _require_id(next(iter(lib.albums())).id)
    during: list[list[str]] = []
    real_trash_album = delete_mod.trash_album  # type: ignore[attr-defined]  # re-export

    def _looks(*args: Any, **kwargs: Any) -> str:
        during.append(sorted(p.name for p in music.iterdir()))
        answer: str = real_trash_album(*args, **kwargs)
        return answer

    monkeypatch.setattr(delete_mod, "trash_album", _looks)

    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert during == [["Art"]], "nothing of ours was planted in the music root"


def test_a_directory_swapped_after_the_check_gets_no_keep_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keep-file is written through the descriptor that was IDENTIFIED.

    The check and the plant are two syscalls, so a rename between them makes a
    path-spelled plant write into whatever now answers to the name. Measured:
    with ``os.open(os.path.join(path, KEEP_NAME))`` instead of ``dir_fd=fd``
    the whole file stayed green, so this shape is the only reader the anchoring
    has. The swap is done from a wrapper around the identity check, which is
    exactly the window a real racer has.
    """
    from app.beets import delete as delete_mod
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    real_check = delete_mod.open_if_one_of_ours  # type: ignore[attr-defined]  # re-export
    moved_to = music / "Downloads" / "the-real-inbox"

    def _swaps(root: Any, protected: ProtectedTrees) -> int | None:
        fd = real_check(root, protected)
        if fd is not None and Path(str(root)) == inbox:
            inbox.rename(moved_to)  # the descriptor still names the real store
            inbox.mkdir()  # a stranger's directory takes the name
            shutil.move(str(moved_to / "Art"), str(inbox / "Art"))  # rows stay valid
        return fd

    monkeypatch.setattr(delete_mod, "open_if_one_of_ours", _swaps)

    album_id = _require_id(next(iter(lib.albums())).id)
    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(trash.rglob("*.mp3")), "the delete happened"
    assert moved_to.is_dir(), "the real store is there"
    assert list(moved_to.iterdir()) == [], "and its keep-file came off the descriptor"
    assert not inbox.exists(), "nothing was written at the swapped-in name, so beets' prune took it"


def test_a_symlink_at_a_store_name_is_not_one_of_ours(tmp_path: Path) -> None:
    """``O_NOFOLLOW``: the identity question is about the NAME, not its target.

    Without it a link standing where a store used to be answers with the
    target's identity, and the delete would plant its keep-file through the
    link — into a directory nobody checked. Measured: dropping ``O_NOFOLLOW``
    from the open left every other test in this file green.
    """
    from app.beets.protected import open_if_one_of_ours

    real = tmp_path / "inbox"
    real.mkdir()
    link = tmp_path / "inbox-link"
    link.symlink_to(real)
    trees = protected_trees(
        settings=Settings(inbox_dir=str(real)),
        music_dir=tmp_path / "music",
        beets_dir=tmp_path / "beets",
        trash_dir=tmp_path / "trash",
        origins_dir=tmp_path / "origins",
        library_path=tmp_path / "beets" / "library.db",
    )

    by_name = open_if_one_of_ours(real, trees)
    assert by_name is not None, "the control: the store itself IS one of ours"
    os.close(by_name)

    assert open_if_one_of_ours(link, trees) is None, "a link at the name is not the store"


def test_a_stored_path_holding_a_nul_byte_is_not_one_of_ours(tmp_path: Path) -> None:
    """The ``ValueError`` beside the ``OSError``, which had no pin.

    ``os.open`` raises ``ValueError`` — not ``OSError`` — for an embedded NUL,
    and the walk that feeds this reads stored row paths. Raising past the caller
    would abandon every descriptor it already holds open.
    """
    from app.beets.protected import open_if_one_of_ours

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    trees = protected_trees(
        settings=Settings(inbox_dir=str(inbox)),
        music_dir=tmp_path / "music",
        beets_dir=tmp_path / "beets",
        trash_dir=tmp_path / "trash",
        origins_dir=tmp_path / "origins",
        library_path=tmp_path / "beets" / "library.db",
    )

    ours = open_if_one_of_ours(inbox, trees)
    assert ours is not None, "the control: this path IS one of ours"
    os.close(ours)

    assert open_if_one_of_ours(f"{inbox}\x00/Art", trees) is None


def test_a_leftover_keep_file_is_adopted_and_left_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed run's keep-file is the next delete's, and it stays its own.

    The plant is ``O_EXCL``, so an existing file means someone else's: this
    delete did not create it and does not remove it. That is what bounds the
    litter a crash can leave to ONE file per app-owned directory — the reason
    the name is fixed rather than a token.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    inbox.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, inbox)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    leftover = inbox / ".musicdrop-keep"
    leftover.write_text("from a run that was killed", encoding="utf-8")

    album_id = _require_id(next(iter(lib.albums())).id)
    delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)

    assert leftover.read_text(encoding="utf-8") == "from a run that was killed"
    assert sorted(p.name for p in inbox.iterdir()) == [".musicdrop-keep"]


def test_a_clutter_list_that_matches_the_keep_file_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A user ``clutter:`` pattern the keep-file matches turns the guard off.

    beets treats a clutter-only directory as empty and rmtrees it whole, so the
    keep-file stops holding the prune off and the store goes — MEASURED here, not
    argued. Nothing in the config is overridden: it is the operator's list, and
    the delete they asked for still happens. The log is what makes it findable.
    """
    import beets

    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    was = beets.config["clutter"].get()
    beets.config["clutter"] = [".musicdrop-*"]

    album_id = _require_id(next(iter(lib.albums())).id)
    try:
        with caplog.at_level(logging.WARNING, logger="app.beets.delete"):
            result = delete_album(
                lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees
            )
    finally:
        beets.config["clutter"] = was

    assert result.trashed_albums == 1
    assert not inbox.exists(), "clutter-only to beets, so the store went with the prune"
    assert [r.getMessage() for r in caplog.records] == [
        "clutter: matches .musicdrop-keep, so MusicDrop's own directories may not be"
        " protected from beets' prune during a delete",
        # The second line is the same fact from the other end: the ``finally``
        # cannot unlink a keep-file whose directory beets took.
        f"could not remove .musicdrop-keep in {inbox}",
    ]


def test_a_store_that_cannot_be_written_in_is_not_protected_and_the_delete_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A read-only store cannot hold a keep-file. The delete still happens.

    The honest outcome, and the one the log names: refusing the delete would deny
    the operator the only thing they asked for, over a directory MusicDrop is
    merely trying to be careful with.

    The store is an ANCESTOR at mode 0o555, so the plant's ``O_CREAT`` fails
    EACCES while the album's own folder below it stays writable and its files
    still move.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this fault needs")

    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    inbox = music / "Downloads" / "inbox"
    folder = inbox / "Art" / "Alb"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(inbox))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    album_id = _require_id(next(iter(lib.albums())).id)
    inbox.chmod(0o555)

    try:
        with caplog.at_level(logging.WARNING, logger="app.beets.delete"):
            result = delete_album(
                lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees
            )
    finally:
        with contextlib.suppress(OSError):
            inbox.chmod(0o755)

    assert result.trashed_albums == 1
    assert list(lib.albums()) == []
    assert list(trash.rglob("*.mp3")), "the delete really happened"
    # The LOG is this test's pin, not the store's survival: a read-only parent
    # stops beets' own ``rmtree`` too, so ``inbox.is_dir()`` holds with the keep
    # logic switched off entirely (measured by the code seat).
    assert [r.getMessage() for r in caplog.records] == [
        f"not protected from beets' prune, cannot write in it: {inbox}"
    ]


def test_a_ghost_delete_keeps_a_trash_that_sits_inside_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Trash root itself is one of ours, and a prune climbed through it.

    Rows naming files inside a Trash that sits INSIDE the music dir, with the
    files gone. ``Album.move`` prunes nothing for a ghost — ``Item.move`` returns
    before its prune when the source is missing, and ``move_art`` returns on a
    missing/absent cover — so the climb was the app's OWN ``prune_dirs`` on the
    album folder, which walks to ``lib.directory`` and rmtree'd the Trash root:
    measured, ``trash root still a dir: False``. Every other route refuses to
    remove the Trash (``empty_one``, ``empty_all``, restore); this one did it
    without asking. The fix is that the app no longer prunes here at all —
    :func:`~app.beets.sidecars.carry_sidecars` re-prunes only the directory of an
    item that really MOVED, and a ghost moves none.

    Trash inside the library is an allowed layout — ``store_layout`` refuses
    ``music`` inside ``trash``, not the other way round — so this is a shape a
    deployment reaches, not a contrived one.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    trash = music / "Trash"
    entry = trash / "Art - Alb"
    entry.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, entry)
    # A bystander with its files really there, so the ghost is the ONLY thing
    # missing: without it ``require_library_root`` refuses the delete for a
    # library whose every sampled file is gone, which is a different test.
    bystander = music / "Bystander" / "Album"
    bystander.mkdir(parents=True)
    _add_album(lib, bystander)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(tmp_path / "absent-inbox"))
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    assert protected_match(trash, trees) is not None, "the Trash must be in the set"
    for track in entry.iterdir():  # removed outside MusicDrop: rows only
        track.unlink()

    ghost = next(a for a in lib.albums() if str(entry) in os.fsdecode(next(iter(a.items())).path))
    delete_album(lib, _require_id(ghost.id), trash_dir=trash, origins_dir=origins, protected=trees)

    assert len(list(lib.albums())) == 1, "the ghost rows went"
    assert trash.is_dir(), "the Trash root did not"
    # The entry, not only the root: with the old prune put back, the Trash root
    # still ends up standing — its keep-file holds it — and the difference that
    # remains is the folder BELOW it. Measured: without this
    # line, re-adding ``prune_dirs(album_root, lib.directory)`` leaves this test
    # green. A ghost relocated nothing, so nothing here has a folder to tidy.
    assert entry.is_dir(), "nothing pruned the folder the rows named"
