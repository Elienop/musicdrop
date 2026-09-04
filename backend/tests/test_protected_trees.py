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
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.beets.library import _require_id
from app.beets.protected import (
    ProtectedTreeError,
    ProtectedTrees,
    app_owned_dirs,
    export_dir,
    open_checked_dir,
    protected_match,
    protected_trees,
    refuse_protected_tree,
)
from app.beets.trash import trash_folder
from app.beets.trash_manage import empty_all, empty_one
from app.config import Settings, settings
from tests.conftest import beets_dir_for, make_test_handle, origins_for, protected_for

# --------------------------------------------------------------------------
# The set itself: it must name the same directories the app's own resolvers do.
# --------------------------------------------------------------------------


def _store_dirs(beets_dir: Path) -> dict[str, Path]:
    """What each real resolver answers, for the settings in force right now."""
    from app.acquisition.inbox import resolve_inbox_dir
    from app.api.bank import get_bank_dir
    from app.api.plex import get_plex_store
    from app.api.slskd import get_slskd_store
    from app.playlists.store import get_playlists_dir

    class _Handle:
        pass

    handle = _Handle()
    handle.beets_dir = beets_dir  # type: ignore[attr-defined]  # only field inbox reads
    return {
        "the import bank": get_bank_dir(),
        "the Plex settings store": get_plex_store()._path.parent,
        "the slskd settings store": get_slskd_store()._path.parent,
        "the playlist store": get_playlists_dir(),
        "the inbox": resolve_inbox_dir(settings, handle),  # type: ignore[arg-type]  # ditto
    }


@pytest.mark.parametrize("configured", [False, True])
def test_app_owned_dirs_name_what_the_five_resolvers_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    """The guard's table is a COPY of five resolvers, so a copy is what is pinned.

    ``app.beets.protected`` is a leaf on purpose — importing ``app.api.bank``
    from it closes the cycle ``store_layout -> api.bank -> beets.duplicates ->
    store_layout``, measured by reading those modules' imports — so the five
    two-line rules are written down twice. This is the test that fails when one
    side moves: both the unset default and a configured override.
    """
    beets_dir = (tmp_path / "beets").resolve()
    beets_dir.mkdir()
    monkeypatch.setattr("app.config.settings.beets_dir", str(beets_dir))
    fields = {
        "the import bank": "bank_dir",
        "the Plex settings store": "plex_settings_dir",
        "the slskd settings store": "slskd_settings_dir",
        "the playlist store": "playlists_dir",
        "the inbox": "inbox_dir",
    }
    if configured:
        for field in fields.values():
            elsewhere = (tmp_path / "elsewhere" / field).resolve()
            elsewhere.mkdir(parents=True)
            monkeypatch.setattr(f"app.config.settings.{field}", str(elsewhere))

    mine = {name: path for path, name, _setting in app_owned_dirs(settings, beets_dir)}
    assert mine == _store_dirs(beets_dir)


@pytest.mark.parametrize("configured", [False, True])
def test_export_dir_names_what_export_dir_for_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    """The sixth copy, whose original takes a beets ``Library`` rather than settings."""
    from app.playlists.reexport import export_dir_for

    music = (tmp_path / "music").resolve()
    music.mkdir()
    if configured:
        elsewhere = (tmp_path / "exports").resolve()
        elsewhere.mkdir()
        monkeypatch.setattr("app.config.settings.playlists_export_dir", str(elsewhere))

    class _Lib:
        directory = os.fsencode(str(music))

    assert export_dir(settings, music) == export_dir_for(_Lib())


def test_every_directory_the_rule_is_about_joins_the_set(tmp_path: Path) -> None:
    """The membership list itself, one row at a time.

    Without this, dropping any single entry from ``protected_trees`` leaves the
    suite green — measured: removing the playlist-exports row survived the whole
    file. ``trash`` is asserted beside them because ``empty_all`` fstat-compares
    against it and a ``None`` there turns that compare off.
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


def test_a_path_that_is_not_there_yet_has_no_identity(tmp_path: Path) -> None:
    """An uncreated store drops out rather than matching everything.

    The complement of the walk: a ``None`` identity must not collapse into a
    key several absent paths share, which is what makes the guard silent on a
    fresh install where ``bank/``, ``plex/`` and ``inbox/`` do not exist yet.
    """
    beets = tmp_path / "beets"
    beets.mkdir()
    trees = protected_trees(
        settings=Settings(),
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


def _trees_for(music: Path) -> ProtectedTrees:
    """A set holding exactly ``music``, so a test's assertion is about one id."""
    return protected_trees(
        settings=Settings(),
        music_dir=music,
        beets_dir=music.parent / "absent-beets",
        trash_dir=music.parent / "absent-trash",
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
    The residual is stated in :mod:`app.beets.protected`.
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
    with pytest.raises(ProtectedTreeError, match="is not the directory MusicDrop checked"):
        open_checked_dir(link, None)


def test_open_checked_dir_refuses_an_identity_that_moved(tmp_path: Path) -> None:
    """A directory replaced by a DIFFERENT one keeps the path and loses the inode."""
    trash = tmp_path / "trash"
    trash.mkdir()
    stale = os.stat(trash)
    trash.rmdir()
    (tmp_path / "trash").mkdir()
    with pytest.raises(ProtectedTreeError, match="changed between the check and the open"):
        open_checked_dir(tmp_path / "trash", (stale.st_dev, stale.st_ino))


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

    with pytest.raises(ProtectedTreeError, match="changed between the check and the open"):
        empty_all(trash, origins_dir=origins_for(trash), protected=trees)
    assert (trash / "Impostor").is_dir()


def test_empty_all_enumerates_from_the_descriptor_it_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half of ``security-auditor-6`` no outcome assertion can see otherwise.

    The directory at the Trash path is REPLACED between the ``open`` and the
    first read — the window itself, forced deterministically by wrapping
    ``open_checked_dir``. Enumerating from the fd yields the entry the check
    approved (``Album``, which the swapped-in directory does not hold, so the
    removal fails and names it); enumerating from the path yields ``Decoy`` and
    removes it. Measured: with ``os.scandir(trash_dir)`` in place of
    ``os.scandir(fd)`` the rest of this file stays green.

    What it does NOT close is stated in ``empty_all``: the per-entry paths are
    still built from ``trash_dir``, so the removal targets follow the swap.
    """
    import app.beets.trash_manage as manage

    trash = tmp_path / "trash"
    (trash / "Album").mkdir(parents=True)
    decoy = tmp_path / "decoy"
    (decoy / "Decoy").mkdir(parents=True)
    real_open = open_checked_dir

    def swapping(path: Path, expected: tuple[int, int] | None) -> int:
        fd = real_open(path, expected)
        os.rename(path, tmp_path / "gone")
        os.rename(decoy, path)
        return fd

    monkeypatch.setattr(manage, "open_checked_dir", swapping)
    with pytest.raises(manage.TrashEmptyPartialError, match="'Album'"):
        empty_all(trash, origins_dir=origins_for(trash), protected=_trees_for(tmp_path / "music"))
    assert (trash / "Decoy").is_dir()


def test_open_checked_dir_returns_a_usable_descriptor(tmp_path: Path) -> None:
    """The control: the ordinary case yields the fd ``empty_all`` enumerates from."""
    trash = tmp_path / "trash"
    trash.mkdir()
    (trash / "Album").mkdir()
    st = os.stat(trash)
    fd = open_checked_dir(trash, (st.st_dev, st.st_ino))
    try:
        assert sorted(entry.name for entry in os.scandir(fd)) == ["Album"]
    finally:
        os.close(fd)


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
    trees = _trees_for(trash / "Sneak")

    with pytest.raises(ProtectedTreeError) as caught:
        empty_all(trash, origins_dir=origins_for(trash), protected=trees)

    assert "'Sneak' is the music library" in str(caught.value)
    assert "Removed 1" in str(caught.value)
    assert (trash / "Sneak").is_dir()
    assert not (trash / "Ordinary").exists()


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

    with pytest.raises(ProtectedTreeError, match="'Sneak' is the music library"):
        empty_one(str(trash / "Sneak"), origins_dir=origins_for(trash), protected=trees)
    assert (trash / "Sneak" / "01.flac").exists()

    assert (
        empty_one(str(trash / "Ordinary"), origins_dir=origins_for(trash), protected=trees).removed
        == 1
    )


def test_trash_folder_refuses_a_husk_that_holds_the_inbox(tmp_path: Path) -> None:
    """The orphan sweep's mover, on a shape nothing else stops today.

    ``_ignore_dirs`` (``api/reorganize.py``) excludes the export dir, the origin
    store, the beets dir and the database's folder — not the inbox — and the
    layout rule has no row for an inbox inside the music library. So
    ``MUSICDROP_INBOX_DIR=<music>/Downloads/inbox`` leaves ``Downloads``
    audio-free and reportable, and the mover takes its whole subtree.

    The control is the same husk with the inbox moved out: it IS trashed.
    """
    trash = tmp_path / "trash"
    music = tmp_path / "music"
    husk = music / "Downloads"
    (husk / "inbox").mkdir(parents=True)
    (husk / "poster.jpg").write_bytes(b"x")
    trees = protected_trees(
        settings=Settings(inbox_dir=str(husk / "inbox")),
        music_dir=music,
        beets_dir=tmp_path / "absent-beets",
        trash_dir=trash,
        origins_dir=origins_for(trash),
        library_path=tmp_path / "absent" / "library.db",
    )

    with pytest.raises(ProtectedTreeError, match="'Downloads' contains the inbox"):
        trash_folder(husk, trash_dir=trash, origins_dir=origins_for(trash), protected=trees)
    assert (husk / "poster.jpg").exists()

    (husk / "inbox").rmdir()
    dest = trash_folder(husk, trash_dir=trash, origins_dir=origins_for(trash), protected=trees)
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
    album_id = _require_id(next(iter(lib.albums())).id)
    with pytest.raises(ProtectedTreeError, match="'Kid A' contains the inbox"):
        delete_album(
            lib,
            album_id,
            trash_dir=trash,
            origins_dir=origins_for(trash),
            protected=protected_for(lib, trash_dir=trash, origins_dir=origins_for(trash)),
        )
    assert len(list(lib.albums())) == 1
    assert (folder / "01 Track.mp3").exists()

    (folder / "inbox").rmdir()
    delete_album(
        lib,
        album_id,
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(lib, trash_dir=trash, origins_dir=origins_for(trash)),
    )
    assert list(lib.albums()) == []


# --------------------------------------------------------------------------
# The routes.
# --------------------------------------------------------------------------


def _trash_dir(client: TestClient) -> Path:
    return Path(client.get("/api/trash").json()["trash_path"])


def test_the_empty_routes_answer_503_and_name_the_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both remove routes, end to end, through production's own set.

    Nothing is patched but a setting: with ``MUSICDROP_INBOX_DIR`` pointing
    inside a Trash entry, ``checked_protected_trees`` finds it by itself. The
    ordinary entry beside it is the control — Empty all removes that one and
    reports it in the same message.
    """
    trash = _trash_dir(client)
    (trash / "Sneak" / "inbox").mkdir(parents=True)
    (trash / "Sneak" / "keep.flac").write_bytes(b"x")
    (trash / "Ordinary").mkdir()
    monkeypatch.setattr("app.config.settings.inbox_dir", str(trash / "Sneak" / "inbox"))

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
    ``continue`` skips ONE candidate rather than ending the loop. The refused
    one is an inbox inside the library, which the sweep's exclusion list does
    not cover (see :func:`test_trash_folder_refuses_a_husk_that_holds_the_inbox`).
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


# --------------------------------------------------------------------------
# The alias no spelled rule can see.
# --------------------------------------------------------------------------

_BIND_PROBE = """
import subprocess, sys
from pathlib import Path

from app.beets.library import _require_id
from app.beets.protected import (
    ProtectedTreeError, ProtectedTrees, protected_trees, refuse_protected_tree,
)
from app.beets.store_layout import StoreLayoutError, check_store_layout
from app.beets.trash_manage import empty_all
from app.config import Settings

base = Path(sys.argv[1])
src = base / "src"
(src / "music" / "Album").mkdir(parents=True)
(src / "music" / "Album" / "01.flac").write_bytes(b"x")
(src / "data").mkdir()
(src / "data" / "library.db").write_bytes(b"db")
M, B = src / "music", src / "data"
T, O = base / "trash", base / "origins"
T.mkdir(); O.mkdir()

# -v /srv/music/musicdrop:/data/beets — the beets dir reached from inside M.
inside = M / "musicdrop"
inside.mkdir()
subprocess.run(["mount", "--bind", str(B), str(inside)], check=True)
assert inside.samefile(B), "the fixture did not alias the beets dir"

def layout():
    try:
        check_store_layout(music_dir=M, beets_dir=B, trash_dir=T, origins_dir=O,
                           library_path=B / "library.db")
        return "ALLOWED"
    except StoreLayoutError:
        return "REFUSED"

def trees():
    return protected_trees(settings=Settings(), music_dir=M, beets_dir=B, trash_dir=T,
                           origins_dir=O, library_path=B / "library.db")

print("spelled-rule", layout())
try:
    refuse_protected_tree(inside, trees(), action="moved")
    print("mover ALLOWED")
except ProtectedTreeError as exc:
    print("mover REFUSED")

# The same alias as a Trash entry, so Empty Trash is what walks it.
entry = T / "Looks Like An Album"
entry.mkdir()
subprocess.run(["mount", "--bind", str(B), str(entry)], check=True)
try:
    empty_all(T, origins_dir=O, protected=ProtectedTrees(ids={}, trash=None))
    print("control-empty-all RAN")
except ProtectedTreeError:
    print("control-empty-all REFUSED")
except Exception as exc:
    # rmtree unlinks the CONTENTS and then fails to rmdir the mount point
    # itself (EBUSY), so the loss lands before the error does.
    print("control-empty-all", type(exc).__name__)
print("control-lost-the-db", not (B / "library.db").exists())

(B / "library.db").write_bytes(b"db")
try:
    empty_all(T, origins_dir=O, protected=trees())
    print("guarded-empty-all RAN")
except ProtectedTreeError:
    print("guarded-empty-all REFUSED")
print("db-survives", (B / "library.db").exists())
"""


def _unshare_works() -> bool:
    """Whether this box grants an unprivileged mount namespace."""
    try:
        done = subprocess.run(
            ["unshare", "-Urm", "true"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


@pytest.mark.skipif(not _unshare_works(), reason="no unprivileged mount namespace on this box")
def test_a_bind_mounted_beets_dir_is_refused_where_the_spelled_rule_allows_it(
    tmp_path: Path,
) -> None:
    """D1's invariant, and the control that shows why the guard is not redundant.

    One child process, four measurements: ``check_store_layout`` ALLOWS
    ``-v /srv/music/musicdrop:/data/beets`` (a mount point's ancestors are its
    own, so no "contains" row can reach the source); the mover's guard refuses
    it; ``empty_all`` with an EMPTY set — this tree before the guard — deletes
    ``library.db`` through a Trash entry aliased the same way; and with the real
    set it refuses and the database survives.
    """
    work = tmp_path / "work"
    work.mkdir()
    script = tmp_path / "probe.py"
    script.write_text(_BIND_PROBE, encoding="utf-8")
    backend = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(backend)}
    done = subprocess.run(
        ["unshare", "-Urm", sys.executable, str(script), str(work)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(backend),
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line and not line.startswith(" ")]
    assert "spelled-rule ALLOWED" in lines, done.stdout
    assert "mover REFUSED" in lines, done.stdout
    assert "control-empty-all TrashEmptyPartialError" in lines, done.stdout
    assert "control-lost-the-db True" in lines, done.stdout
    assert "guarded-empty-all REFUSED" in lines, done.stdout
    assert "db-survives True" in lines, done.stdout


def test_the_guard_reads_identity_not_the_spelling(tmp_path: Path) -> None:
    """The same question without a mount namespace, through a hard link's inode.

    A directory cannot be hard-linked, so this uses the one alias every
    filesystem gives: ``..`` and a doubled separator resolve to one inode while
    spelling differently. Weaker than the namespace test above and here so the
    property has a pin on a box that cannot ``unshare``.
    """
    music = tmp_path / "music"
    music.mkdir()
    trees = _trees_for(music)
    spelled_otherwise = Path(str(tmp_path) + "//music/./../music")
    assert str(spelled_otherwise) != str(music)
    assert protected_match(spelled_otherwise, trees) is not None


def test_the_empty_set_refuses_nothing(tmp_path: Path) -> None:
    """The control for every assertion above: with no identities, nothing matches."""
    music = tmp_path / "music"
    music.mkdir()
    empty = ProtectedTrees(ids={}, trash=None)
    assert protected_match(music, empty) is None
    refuse_protected_tree(music, empty, action="moved")
