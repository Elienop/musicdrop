"""Tests for the shared reversible-trash primitive + read helpers."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from beets.library import Item, Library
from fastapi import HTTPException

from app.beets import trash as trash_mod
from app.beets.library import (
    LibraryHandle,
    LibraryRootUnavailableError,
    _require_id,
    require_library_root,
)
from app.beets.trash import (
    TrashMoveIncompleteError,
    _album_root,
    _trash_container_name,
    album_folder,
    album_format_bitrate,
    safe_container_name,
    trash_album,
)
from app.beets.trash_origins import TrashOriginsStoreUnusableError
from app.wire import PLACEHOLDER
from tests.conftest import (
    beets_dir_for,
    build_library,
    make_test_handle,
    origins_for,
    protected_for,
)


def test_trash_album_moves_files_and_drops_db(duplicates_lib: Library, tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)

    with duplicates_lib.transaction():
        trash_path = trash_album(
            duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash)
        )

    # DB row dropped; files relocated under Trash, not destroyed.
    assert duplicates_lib.get_album(album_id) is None
    assert str(trash) in trash_path
    assert os.path.isdir(trash_path)


def test_safe_container_name_keeps_a_display_name_to_one_listable_level() -> None:
    """The name comes from a display string, so four characters have to go.

    A separator would nest the container out of the Trash listing (only
    top-level entries are listed) and off its own origin record's key; a leading
    dot is skipped by the listing while Empty-all still removes it; a NUL raises
    ValueError at the mkdir, which no ``except OSError`` catches; U+FFFD is the
    display form of a byte no path can spell, so it collides with a damaged
    sibling.
    """
    assert safe_container_name("AC/DC", " - artist image") == "AC_DC - artist image"
    assert safe_container_name(".hack", " - artist image") == "hack - artist image"
    assert safe_container_name("A\x00B", " - artist image") == "A_B - artist image"
    assert safe_container_name("..", " - artist image") == "artist - artist image"
    # U+FFFD is what ``wire_safe`` puts in place of an undecodable byte, so a
    # container spelling it displays the same as a damaged sibling's name and
    # ``wire._match_display_child`` refuses BOTH rows with a 409.
    assert safe_container_name("A\ufffdB", " - artist image") == "A_B - artist image"
    # The ordinary name is untouched — a sanitizer that rewrote every name would
    # rename every container and nothing above would notice.
    assert safe_container_name("ABBA", " - artist image") == "ABBA - artist image"


def test_a_tag_built_container_name_is_neutralised_like_a_display_name(
    tmp_path: Path,
) -> None:
    """The album-delete name comes from TAGS, which can spell the same four.

    ``albumartist``/``album`` are library text, and a literal U+FFFD in one used
    to reach the Trash dir: that entry displays identically to a damaged
    sibling's name and ``wire._match_display_child`` then answers 409 on BOTH
    rows, so neither can be restored or emptied. A NUL reached ``dest.mkdir()``
    as a ``ValueError`` no ``except OSError`` catches. One replace set now, so a
    tag cannot spell what a display name is protected from.
    """
    for artist, title in ((f"AB{PLACEHOLDER}BA", "Gold"), ("a\x00b", "x")):
        name = _trash_container_name(SimpleNamespace(albumartist=artist, album=title))

        assert os.sep not in name
        assert "/" not in name
        assert "\x00" not in name
        assert PLACEHOLDER not in name
        # Path-spellable, one level, and the listing (which reads top-level
        # entries only) can see it.
        (tmp_path / name).mkdir()
        assert [e.name for e in tmp_path.iterdir()] == [name]
        assert os.fsdecode(os.fsencode(name)) == name
        (tmp_path / name).rmdir()

    # A leading dot too: ``trash_manage._audio_free_entries`` skips a
    # dot-leading entry that ``empty_all`` still removes.
    assert _trash_container_name(SimpleNamespace(albumartist=".hack", album="Gold")) == (
        "hack - Gold"
    )
    # Untouched for a tag that spells none of them and leads with no dot - every
    # existing entry name has to keep its spelling, or the origin records stop
    # matching.
    assert _trash_container_name(SimpleNamespace(albumartist="ABBA", album="Gold")) == (
        "ABBA - Gold"
    )
    assert _trash_container_name(SimpleNamespace(albumartist=None, album=None)) == "album"


def test_album_format_bitrate_reads_first_item(duplicates_lib: Library) -> None:
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    fmt, kbps = album_format_bitrate(list(album.items()))
    # The placeholder files carry no real audio header, so format/bitrate are
    # absent — the helper must degrade to (None, None), never raise.
    assert fmt is None or isinstance(fmt, str)
    assert kbps is None or isinstance(kbps, int)


def test_album_folder_is_dirname_of_first_item(duplicates_lib: Library) -> None:
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    folder = album_folder(duplicates_lib, list(album.items()))
    assert folder.endswith("Discovery")


def test_an_album_whose_every_row_is_in_trash_has_no_origin_to_show(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """``not_in`` can exclude EVERY row, and then there is no folder to name.

    The part-way retry is the shape that reaches it — the first attempt moved
    what it could, and a second call sees only Trash rows. Falling back to the
    rows themselves would write an origin naming a path inside Trash, which is
    the record the round-2 fix removed. ``trash_album`` gates on
    ``source_root and moved``, so ``""`` means no record rather than a bad one.
    """
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    items = list(album.items())
    real_root = _album_root(duplicates_lib, items)
    assert real_root, "the control: with no exclusion it answers the album's folder"

    assert _album_root(duplicates_lib, items, not_in=Path(real_root).parent) == ""


class _StubApp:
    """The minimum an ``_op`` reads off ``request.app``: ``state.beets_library``.

    ``_swap_lock`` creates its lock lazily on whatever state object it is handed
    (config_editor.py:624-630) and ``_settings`` falls back to the module
    singleton when ``state.settings`` is absent (:632-645), so a bare namespace
    is enough — no TestClient, no lifespan, no real app.
    """

    def __init__(self, handle: LibraryHandle) -> None:
        self.state = SimpleNamespace(beets_library=handle)


class _StubRequest:
    def __init__(self, app: _StubApp) -> None:
        self.app = app


# ----- The unmounted-share guard: a missing folder must not become a row drop -----
#
# The row-dropping primitive here is ``trash_album``, whose per-item
# ``Album.move`` SILENTLY skips missing sources (beets 2.12 onward;
# library/models.py:1197-1211), so an absent path reads as "the user deleted
# this". With the library root itself gone that reading is wrong for EVERY album,
# so the shared root predicate has to run before the first mutation. It matters
# doubly because a beets ``Transaction`` COMMITS on the way out even when it is
# unwinding an exception (dbcore/db.py:940-957, no rollback branch): anything
# dropped before the raise would stick.


def test_trash_album_root_present_but_empty_raises(duplicates_lib: Library, tmp_path: Path) -> None:
    """The dropped-NAS signature: the mountpoint dir stays, its contents vanish.

    ``os.path.isdir`` is still True here, so an isdir-only guard would wave this
    through and the ghost arm would drop the rows. Pins that the FULL shared
    predicate (missing OR empty OR unreadable) is what runs — not a parallel
    re-implementation. The twin below removes the root outright; this one is the
    shape only the full predicate can tell from a healthy library.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    root = os.fsdecode(duplicates_lib.directory)
    shutil.rmtree(root)
    os.mkdir(root)  # present but empty
    assert os.path.isdir(root)

    origins = origins_for(trash)
    tx = duplicates_lib.transaction()
    with pytest.raises(LibraryRootUnavailableError), tx:
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins)

    assert duplicates_lib.get_album(album_id) is not None
    assert not trash.exists()


def test_trash_album_root_gone_raises_before_any_mutation(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The second primitive (duplicates resolve / import Replace) aborts too.

    ``trash_album`` would otherwise ``mkdir`` the Trash container, let beets skip
    every missing-source move without a word, and then drop the rows — a trash
    path pointing at an empty folder. The guard sits ahead of the mkdir, so the
    never-created trash dir is what discriminates guard placement.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    origins = origins_for(trash)
    tx = duplicates_lib.transaction()
    with pytest.raises(LibraryRootUnavailableError), tx:
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins)

    assert duplicates_lib.get_album(album_id) is not None
    assert not trash.exists()  # the guard preceded even the container mkdir


def test_duplicates_resolve_surfaces_the_root_cause(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The resolve caller inherits the guard, and its 500 names the real cause.

    Lives beside the primitive rather than in test_duplicates_resolve.py because
    what it pins is ``trash_album``'s raise reaching a caller honestly: the op's
    blanket ``except`` interpolates the exception into ``message``, so the user
    reads "Is the music share mounted?" instead of a Trash-recovery promise for
    files that never moved.
    """
    import asyncio

    from app.beets.duplicates import resolve_duplicate_group, resolve_duplicates_op
    from app.models.duplicates import DuplicateMode, ResolveRequest

    group = [a for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]
    keep, drop = _require_id(group[0].id), _require_id(group[1].id)
    n_before = len(list(duplicates_lib.albums()))
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    with pytest.raises(LibraryRootUnavailableError):
        resolve_duplicate_group(
            duplicates_lib,
            mode=DuplicateMode.strict,
            keep_album_id=keep,
            remove_album_ids=[drop],
            trash_dir=tmp_path / "trash",
            origins_dir=tmp_path / "trash-origins",
        )
    assert len(list(duplicates_lib.albums())) == n_before

    req = _StubRequest(_StubApp(make_test_handle(duplicates_lib, beets_dir_for(tmp_path))))
    resolve_req = ResolveRequest(
        mode=DuplicateMode.strict, keep_album_id=keep, remove_album_ids=[drop]
    )
    coro = resolve_duplicates_op(
        req,  # type: ignore[arg-type]  # duck-typed stub: only .app.state is read
        resolve_req,
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)
    assert ei.value.status_code == 500  # the established absorb shape, message-carrying
    assert "music share mounted" in str(ei.value.detail)
    assert len(list(duplicates_lib.albums())) == n_before


def test_root_unreadable_names_permissions_not_emptiness(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An I/O failure listing the root is not the same fault as an empty root.

    Both fail closed, but only one of the two sentences tells the person who can
    fix it what to look at — a PUID/PGID drift, a stale NFS handle or an EIO
    reads as "the folder is empty" otherwise, which sends them looking for
    missing files that are all still there.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)

    def _denied(path: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "scandir", _denied)

    origins = origins_for(trash)
    tx = duplicates_lib.transaction()
    with pytest.raises(LibraryRootUnavailableError) as ei, tx:
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins)

    assert "unreadable" in str(ei.value)
    assert "empty" not in str(ei.value)
    assert "Permission denied" in str(ei.value)  # the OS's own reason, no path
    assert duplicates_lib.get_album(album_id) is not None  # still fails CLOSED
    assert not trash.exists()


# ----- The guard is a PRE-check; the move needs its own POST-condition -----
#
# Passing the root check does not make the moves happen. The window between the
# check and ``Album.move`` is enough for a share to drop, and beets answers a
# missing source by logging and returning (models.py:1197-1211) — so ``move``
# reports success, nothing is relocated, and ``remove`` drops the rows anyway.
# Verifying the mutation AFTER the fact is what closes that, and it closes every
# cause of a silent skip, not just an unmount.


def test_trash_album_root_vanishing_after_the_check_keeps_rows(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the pre-check cannot win: the share drops the instant it passes."""
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    root = os.fsdecode(duplicates_lib.directory)
    real_guard = require_library_root  # read from its own module; patched on trash_mod
    calls = {"n": 0}

    def _drops_the_instant_it_passes(lib: Library) -> None:
        calls["n"] += 1
        real_guard(lib)  # the pre-check really does pass...
        if calls["n"] == 1:
            shutil.rmtree(root)  # ...and the share goes away right after it

    monkeypatch.setattr(trash_mod, "require_library_root", _drops_the_instant_it_passes)

    origins = origins_for(trash)
    tx = duplicates_lib.transaction()
    with pytest.raises(LibraryRootUnavailableError), tx:
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins)

    assert duplicates_lib.get_album(album_id) is not None  # rows kept
    assert list(trash.iterdir()) == []  # and no phantom empty container in Trash


def test_trash_album_refuses_when_the_move_silently_did_nothing(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent skip with a HEALTHY root is still a silent skip.

    An unmount is only the cause we found first. Whatever the reason ``move``
    relocated nothing — a permission fault on the container, a beets change, a
    bug here — dropping the rows would be the same data loss, so the refusal is
    written against the OUTCOME rather than against the unmount.
    """
    from beets.library import Album as BeetsAlbum

    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    monkeypatch.setattr(BeetsAlbum, "move", lambda self, **kwargs: None)

    origins = origins_for(trash)
    tx = duplicates_lib.transaction()
    with pytest.raises(TrashMoveIncompleteError) as ei, tx:
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins)

    assert "Trash" in str(ei.value)
    assert duplicates_lib.get_album(album_id) is not None  # rows kept, honestly
    assert list(trash.iterdir()) == []


def test_trash_album_post_condition_accepts_a_healthy_move(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The false-positive direction: legitimate work must still go through.

    A post-condition that refuses a real move would be worse than the bug it
    closes, so pin the happy path from the same angle the check reads it: every
    file under the returned container, rows gone.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    n_tracks = len(list(album.items()))

    with duplicates_lib.transaction():
        trash_path = trash_album(
            duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash)
        )

    assert Path(trash_path).is_dir()
    assert str(trash) in trash_path  # inside Trash, not the music dir
    assert len(list(Path(trash_path).glob("*.mp3"))) == n_tracks  # the files really moved
    assert duplicates_lib.get_album(album_id) is None  # and only then were rows dropped


def test_trash_album_ghost_files_gone_still_drops_rows(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The post-condition must not eat the deliberate ghost cleanup.

    A ghost album moves nothing for a legitimate reason: its files really are
    gone. The delete route, import Replace and duplicates resolve all reach
    ``trash_album``, and all rely on the rows being dropped here — this arm is
    the only ghost branch there is. Refusing would strand every replaced
    ghost in the library forever — the shape the end-to-end Replace suite
    catches, pinned here at the primitive where the decision is made.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    shutil.rmtree(album_folder(duplicates_lib, list(album.items())))  # files gone, root fine

    with duplicates_lib.transaction():
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert duplicates_lib.get_album(album_id) is None  # ghost rows dropped
    assert list(trash.iterdir()) == []  # and no empty container left in Trash


# ----- A dropped share masked by a stray entry on the local mountpoint -----
#
# ``require_library_root`` treats a root with ANY entry as mounted, so a
# ``.stfolder``/``lost+found``/empty leftover directory sitting on the local
# mountpoint keeps it passing while the share is gone. Both row-dropping ghost
# arms therefore read every album as deleted, and the library goes one delete at
# a time — which is why they ask ``require_library_present`` instead. This is a
# DIFFERENT cause from the post-condition above: there the files were still
# sitting there, here they are all genuinely absent for one shared reason.


def _mountpoint_with_only_a_stray(lib: Library, stray: str = ".stfolder") -> Path:
    root = Path(os.fsdecode(lib.directory))
    shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / stray).mkdir()
    return root


def test_trash_album_refuses_a_dropped_share_masked_by_a_stray(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The per-item ghost arm of the post-condition, same cause.

    ``Album.move`` skips every missing source and returns normally, so nothing
    moves, nothing is present, and the ghost arm would concede — dropping the
    rows of an album whose files are merely unreachable.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    _mountpoint_with_only_a_stray(duplicates_lib)

    origins = origins_for(trash)
    tx = duplicates_lib.transaction()
    with pytest.raises(LibraryRootUnavailableError), tx:
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins)

    assert duplicates_lib.get_album(album_id) is not None  # rows kept
    assert list(trash.iterdir()) == []  # no phantom container left behind


def test_delete_artist_fan_out_stops_on_the_first_masked_drop(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The fan-out must not erase album after album through the masked drop.

    Radiohead holds two albums; a guard that fired only after the first
    ``album.remove`` would leave one row committed and unrecoverable (beets
    commits on the way out of the transaction even while unwinding).

    ``trash.exists()`` is not the assertion it was: the per-item mover
    ``mkdir``s the Trash root before it moves, so what this pins is that no
    ENTRY was created — the refusal lands before any container is filled.
    """
    from app.beets.delete import delete_artist

    trash = tmp_path / "trash"
    total_before = len(list(duplicates_lib.albums()))
    _mountpoint_with_only_a_stray(duplicates_lib, "lost+found")

    origins = origins_for(trash)
    protected = protected_for(duplicates_lib, trash_dir=trash, origins_dir=origins)
    with pytest.raises(LibraryRootUnavailableError):
        delete_artist(
            duplicates_lib,
            "Radiohead",
            trash_dir=trash,
            origins_dir=origins,
            protected=protected,
        )

    assert len(list(duplicates_lib.albums())) == total_before
    assert list(trash.iterdir()) == []
    assert not list(origins.glob("*.json"))


# ----- The same dropped share, under a FLAT path template -----
#
# A ``paths.default`` with no directory component (beets' own ``$title``,
# editable from Settings -> Naming) files every track directly in the music
# root, so the sampler sees a DIFFERENT shape from the tests above: every
# sampled path's directory IS the music root, which the stray keeps alive. The
# drop would happen in ``_require_move_happened``'s ghost arm. Measured on the
# folder-based sampler: 20 album rows became 19 with Trash empty. A test that
# only called ``require_library_present`` would not prove the arm that actually
# fires is guarded.


def _flat_library_on_a_dropped_share(tmp_path: Path) -> tuple[Library, Path]:
    """One flat library, its files placed by beets, then the share drops."""
    music = tmp_path / "music"
    music.mkdir()
    lib = build_library(str(tmp_path / "library.db"), str(music), path_format="$title")

    for a in range(4):
        item = Item(
            album=f"Album {a:03d}",
            albumartist=f"Artist {a:03d}",
            artist=f"Artist {a:03d}",
            title=f"Song {a:03d}",
            track=1,
        )
        item.path = os.fsencode(str(music / f"provisional-{a:03d}.mp3"))
        lib.add_album([item]).store()
        dest = Path(os.fsdecode(item.destination()))
        assert dest.parent == music, f"the template must be flat, got {dest}"
        dest.write_bytes(b"\x00")  # placeholder bytes; tests never read audio
        item.path = os.fsencode(str(dest))
        item.store()

    shutil.rmtree(music)
    music.mkdir()
    (music / ".stfolder").mkdir()
    return lib, music


def test_trash_album_refuses_a_dropped_flat_share_masked_by_a_stray(
    tmp_path: Path,
) -> None:
    """A flat library on a dropped share must keep its rows, like a nested one.

    The regression: while the presence sampler took ``os.path.dirname`` of each
    sampled path, a flat library's every sample WAS the music root — a directory
    the stray ``.stfolder`` keeps alive — so the check accepted and this call
    dropped the album row having moved nothing into Trash.
    """
    trash = tmp_path / "trash"
    lib, music = _flat_library_on_a_dropped_share(tmp_path)
    album = next(iter(lib.albums()))
    album_id = _require_id(album.id)
    total_before = len(list(lib.albums()))

    require_library_root(lib)  # the cheap predicate is happy — this is the gap

    origins = origins_for(trash)
    tx = lib.transaction()
    with pytest.raises(LibraryRootUnavailableError), tx:
        trash_album(lib, album, trash_dir=trash, origins_dir=origins)

    assert lib.get_album(album_id) is not None  # rows kept
    assert len(list(lib.albums())) == total_before
    assert not trash.exists() or list(trash.iterdir()) == []  # nothing reached Trash
    assert list(music.iterdir()) == [music / ".stfolder"]  # and nothing was written back


# ----- The origin-store guard: a store that cannot be used refuses the move -----
#
# Owner ruling, ``decisions.md`` 28: a delete that cannot record where a folder
# came from must not run. Until it, a store the app could not use read as EMPTY
# and the delete completed — handing out a name whose record was still on disk
# and then losing the new record, so a repaired store offered one folder's
# origin to another. The three movers here all ask before they allocate.


def _file_at_the_store(tmp_path: Path) -> Path:
    """A store path with a regular FILE at it, and the reason it is the fixture.

    Root-proof, which a mode bit is not: as uid 0 a directory at 0000 denies
    nothing and a chmod fixture reports green having exercised no fault. It is
    also the shape nothing else could see — ``Path.exists()`` absorbs the
    ENOTDIR a child of a file raises, so before the store check the allocator
    read "no record here" for it in complete silence.
    """
    origins = origins_for(tmp_path / "trash")
    origins.write_bytes(b"not a directory")
    return origins


def test_trash_album_refuses_an_unusable_store_before_any_mkdir(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The per-item mover, which is also duplicates-resolve's and import Replace's.

    It is the one of the three whose refusal has no test of its own anywhere
    else, and the one whose ``mkdir`` is TWO: ``trash_dir`` and then the album's
    own container under it.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = album_folder(duplicates_lib, list(album.items()))
    _file_at_the_store(tmp_path)

    origins = origins_for(trash)
    tx = duplicates_lib.transaction()
    with pytest.raises(TrashOriginsStoreUnusableError) as ei, tx:
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins)

    assert "trash-origins" in str(ei.value)
    assert duplicates_lib.get_album(album_id) is not None
    assert os.path.isdir(folder), "the album's files must still be where they were"
    assert not trash.exists(), "the refusal comes before the Trash mkdir"


def test_the_delete_route_refuses_an_unusable_store_before_the_GHOST_branch(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The check sits above every branch, including the ones that move nothing.

    A ghost album — rows with no folder — is dropped by a branch that relocates
    not one byte, so a check placed beside the ``mkdir`` would let it through.
    The invariant the owner ruled on is about ROWS, not about files: a fan-out
    that refused its third album having already erased two ghosts would have
    broken it while every file stayed exactly where it was.

    Asked through ``delete_album``, the front door: the whole-folder mover this
    used to drive is gone, and the ghost arm that survives is ``trash_album``'s.
    """
    from app.beets.delete import delete_album

    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    shutil.rmtree(album_folder(duplicates_lib, list(album.items())))  # ghost: rows only
    _file_at_the_store(tmp_path)

    origins = origins_for(trash)
    protected = protected_for(duplicates_lib, trash_dir=trash, origins_dir=origins)
    with pytest.raises(TrashOriginsStoreUnusableError):
        delete_album(
            duplicates_lib, album_id, trash_dir=trash, origins_dir=origins, protected=protected
        )

    assert duplicates_lib.get_album(album_id) is not None, "the ghost rows must survive"
    assert not trash.exists()


def test_a_store_that_does_not_exist_yet_is_created_by_the_delete(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """Absence is the FRESH-INSTALL shape and must not be read as a fault.

    No deployment has an origins directory until its first delete. A guard that
    treated ENOENT like EACCES would refuse every first delete there is, so this
    is the counter-example the check is written against — end to end through
    ``delete_album``, not only at the predicate.
    """
    from app.beets.delete import delete_album

    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    assert not origins_for(trash).exists(), "the fixture must start with no store"

    delete_album(
        duplicates_lib,
        album_id,
        trash_dir=trash,
        origins_dir=origins_for(trash),
        protected=protected_for(duplicates_lib, trash_dir=trash, origins_dir=origins_for(trash)),
    )

    assert duplicates_lib.get_album(album_id) is None, "the delete must have completed"
    assert origins_for(trash).is_dir()
    entry = next(p for p in trash.iterdir() if p.is_dir())
    assert (origins_for(trash) / f"{entry.name}.json").is_file()
