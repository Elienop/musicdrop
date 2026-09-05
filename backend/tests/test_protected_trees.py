"""The identity guard the movers and the remover run at the point of destruction.

``store_layout`` compares SPELLINGS, and a bind mount gives one directory two
of them. The namespace test below measures both halves in one child process:
``check_store_layout`` ALLOWS the aliased layout, and with the guard's set empty
— which is what this tree looked like before — ``empty_all`` deletes the beets
dir through it.
"""

from __future__ import annotations

import logging
import os
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

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
from tests.conftest import beets_dir_for, make_test_handle, origins_for, protected_for

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


def _trees_for(music: Path, trash: Path | None = None) -> ProtectedTrees:
    """A set holding exactly ``music``, so a test's assertion is about one id.

    ``trash`` is passed whenever the test drives ``empty_all``, which refuses a
    Trash identity it could not take.
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

    try:
        with caplog.at_level(logging.WARNING, logger="app.beets.protected"):
            with pytest.raises(TrashEmptyPartialError) as caught:
                empty_all(trash, origins_dir=origins, protected=trees)
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
    """A directory replaced by a DIFFERENT one keeps the path and loses the inode."""
    trash = tmp_path / "trash"
    trash.mkdir()
    trees = _trees_with_trash(trash)
    trash.rmdir()
    (tmp_path / "trash").mkdir()
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

    with pytest.raises(ProtectedTreeError, match="changed between the check and the open"):
        empty_all(trash, origins_dir=origins, protected=trees)
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
        trash, origins_dir=origins_for(trash), protected=_trees_for(tmp_path / "music", trash)
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

    with pytest.raises(ProtectedTreeError) as caught:
        empty_all(trash, origins_dir=origins, protected=trees)

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

    try:
        with pytest.raises(ProtectedTreeError) as caught:
            empty_all(trash, origins_dir=origins, protected=trees)
    finally:
        os.chmod(trash / "stuck", 0o700)

    message = str(caught.value)
    assert "move those entries out of Trash" in message, message
    assert "1 could not be removed ('stuck')" in message, message


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
    with pytest.raises(ProtectedTreeError, match="the Trash directory is the music library"):
        empty_all(trash, origins_dir=origins, protected=trees)
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
    with pytest.raises(ProtectedTreeError, match=f"the Trash directory {phrase}"):
        empty_all(trash, origins_dir=origins, protected=trees)
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
    try:
        with pytest.raises(ProtectedTreeError) as caught:
            empty_all(trash, origins_dir=origins, protected=trees)
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
    trees = _trees_for(trash / "Sneak")
    origins = origins_for(trash)
    sneak = str(trash / "Sneak")

    with pytest.raises(ProtectedTreeError, match="'Sneak' is the music library"):
        empty_one(sneak, origins_dir=origins, protected=trees)
    assert (trash / "Sneak" / "01.flac").exists()

    assert empty_one(str(trash / "Ordinary"), origins_dir=origins, protected=trees).removed == 1


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
    with pytest.raises(
        ProtectedTreeError, match="changed between the check and the removal"
    ) as err:
        empty_all(trash, origins_dir=origins, protected=trees)

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
    with pytest.raises(
        ProtectedTreeError, match="changed between the check and the removal"
    ) as err:
        empty_one(entry, origins_dir=origins, protected=trees)

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

    with pytest.raises(
        ProtectedTreeError, match="changed between the check and the removal"
    ) as err:
        empty_one(entry, origins_dir=origins, protected=trees)

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
    empty = (
        partial(empty_all, trash, origins_dir=origins, protected=trees)
        if path == "all"
        else partial(empty_one, str(trash / "Album"), origins_dir=origins, protected=trees)
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


def test_delete_refuses_an_album_folder_that_holds_an_app_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``trash_album_folder``'s whole-folder branch, which moves a TREE.

    The inbox stands in for the shape a deployment really reaches this by, which
    is the bind-mount alias in the namespace test below: the code path is the
    same and this one needs no mount namespace. A flat library is NOT the case —
    measured, ``_folder_is_shared`` returns True when the album root IS the music
    root (``trash.py``'s first branch), so that layout takes the per-item
    fallback and this guard is never reached.

    The control is the same album with the inbox moved out: the delete lands.
    """
    from app.beets.delete import delete_album
    from tests.conftest import build_library

    music = tmp_path / "music"
    folder = music / "Radiohead" / "Kid A"
    folder.mkdir(parents=True)
    lib = build_library(str(tmp_path / "library.db"), str(music))
    _add_album(lib, folder)
    (folder / "inbox").mkdir()
    monkeypatch.setattr("app.config.settings.inbox_dir", str(folder / "inbox"))

    trash = tmp_path / "trash"
    origins = origins_for(trash)
    album_id = _require_id(next(iter(lib.albums())).id)
    # Built here, while the inbox is still inside the album folder: the set is a
    # snapshot, so the control below has to build its own after the rmdir.
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)
    with pytest.raises(ProtectedTreeError, match="'Kid A' contains the inbox"):
        delete_album(lib, album_id, trash_dir=trash, origins_dir=origins, protected=trees)
    assert len(list(lib.albums())) == 1
    assert (folder / "01 Track.mp3").exists()

    (folder / "inbox").rmdir()
    delete_album(
        lib,
        album_id,
        trash_dir=trash,
        origins_dir=origins,
        protected=protected_for(lib, trash_dir=trash, origins_dir=origins),
    )
    assert list(lib.albums()) == []


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


def test_delete_artist_asks_the_guard_before_it_moves_the_first_album(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fan-out's "Nothing was moved." has to be true of the OPERATION.

    Asked per album inside the loop, an artist whose SECOND album held the store
    answered the partial 500 — "the delete stopped after 1 of 2 albums had been
    moved to Trash (... Nothing was moved.)" — after the first album's folder was
    already in Trash and its rows dropped.

    The control is a FLAT library, where every album root IS the music dir and so
    is in the protected set: those albums take the per-item fallback, which never
    reaches this guard, so a pre-loop check that did not mirror the branch would
    refuse every delete on that layout.
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
    (second / "inbox").mkdir()
    monkeypatch.setattr("app.config.settings.inbox_dir", str(second / "inbox"))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)

    with pytest.raises(ProtectedTreeError, match="'B Second' contains the inbox"):
        delete_artist(lib, "Radiohead", trash_dir=trash, origins_dir=origins, protected=trees)
    assert len(list(lib.albums())) == 2
    assert (first / "01 Track.mp3").exists()
    assert not trash.exists()


def test_delete_artist_still_deletes_on_a_flat_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the pre-loop guard: the album root IS the protected music dir."""
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


@pytest.mark.parametrize("route", ["album", "artist"])
def test_the_delete_ops_build_the_set_they_hand_the_mover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Both ops, through ``_checked_store``, answering 503 with the sentence.

    Measured without this: handing ``delete_album`` an EMPTY set in the op left
    ``test_delete.py`` and this file green, because every other test here calls
    the mover directly. The op is where the set is BUILT, so it needs its own
    pin — one per route, since each has its own arm.
    """
    import asyncio
    from types import SimpleNamespace

    from app.beets.delete import delete_album_op, delete_artist_op
    from tests.conftest import build_library

    music = tmp_path / "music"
    folder = music / "Radiohead" / "Kid A"
    folder.mkdir(parents=True)
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, folder)
    (folder / "inbox").mkdir()
    trash = tmp_path / "trash"
    monkeypatch.setattr("app.config.settings.inbox_dir", str(folder / "inbox"))

    class _App:
        state = SimpleNamespace(
            beets_library=make_test_handle(lib, beets_dir),
            settings=Settings(
                trash_dir=str(trash),
                trash_origins_dir=str(origins_for(trash)),
                inbox_dir=str(folder / "inbox"),
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
    with pytest.raises(HTTPException) as caught:
        asyncio.run(op)
    assert caught.value.status_code == 503
    assert "'Kid A' contains the inbox" in str(caught.value.detail)
    assert "Nothing has been deleted." in str(caught.value.detail)
    assert len(list(lib.albums())) == 1
    assert not trash.exists()


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
    ``test_a_bind_mounted_beets_dir_is_refused_at_the_mover_and_the_remover``,
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
    """The five arguments, asserted directly rather than through an outcome.

    The test above only reaches ``settings=``: it turns on a CONFIGURED inbox,
    so the four path arguments could each be wrong and it would still refuse.
    They are what tie the set to the same music root, beets dir and pair
    ``check_store_layout`` has just accepted, which is the whole point of taking
    it beside that call.
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
    monkeypatch.setattr(
        runner_mod,
        "protected_trees",
        lambda **kwargs: (
            seen.update(kwargs),
            ProtectedTrees(ids={}, trash=None, trash_alias=None),
        )[1],
    )

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
    assert seen["music_dir"] == music
    assert seen["beets_dir"] == handle.beets_dir
    assert seen["trash_dir"] == trash
    assert seen["origins_dir"] == origins_for(trash)
    assert seen["library_path"] == beets_dir / "library.db"


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
    empty = ProtectedTrees(ids={}, trash=None, trash_alias=None)
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
    measured, that removed the inbox AND the folder above it, with nothing in
    the app to recreate either.
    """
    from app.beets.trash import trash_album_folder
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

    album = next(iter(lib.albums()))
    with lib.transaction():
        trash_album_folder(lib, album, trash_dir=trash, origins_dir=origins, protected=trees)

    assert list(lib.albums()) == []
    assert inbox.is_dir(), "the store the guard protects must still be there"
    assert not (inbox / "01 Track.mp3").exists()


def test_an_album_folder_that_holds_a_store_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: a tree HOLDING a store has no safe move, so it is a 503.

    The control for the fall-through above — an ``is`` answer and a ``contains``
    answer must not collapse into one branch.
    """
    from app.beets.trash import trash_album_folder
    from tests.conftest import build_library

    music = tmp_path / "music"
    album_dir = music / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    (album_dir / "inbox").mkdir()
    beets_dir = beets_dir_for(tmp_path)
    lib = build_library(str(beets_dir / "library.db"), str(music))
    _add_album(lib, album_dir)
    monkeypatch.setattr("app.config.settings.inbox_dir", str(album_dir / "inbox"))
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    trees = protected_for(lib, trash_dir=trash, origins_dir=origins)

    album = next(iter(lib.albums()))
    tx = lib.transaction()  # constructing one is inert; the open happens on entry
    with pytest.raises(ProtectedTreeError, match="'Album' contains the inbox"):
        with tx:
            trash_album_folder(lib, album, trash_dir=trash, origins_dir=origins, protected=trees)
    assert len(list(lib.albums())) == 1
    assert (album_dir / "01 Track.mp3").exists()
