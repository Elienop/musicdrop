"""Tests for the shared reversible-trash primitive + read helpers."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from beets.dbcore.query import Query
from beets.dbcore.sort import Sort
from beets.library import Item, Library
from fastapi import HTTPException

from app.beets import trash as trash_mod
from app.beets.library import (
    LibraryHandle,
    LibraryRootUnavailableError,
    _abs_path,
    _require_id,
    require_library_root,
)
from app.beets.trash import (
    TrashMoveIncompleteError,
    _album_root,
    _folder_is_shared,
    album_folder,
    album_format_bitrate,
    trash_album,
    trash_album_folder,
)
from app.beets.trash_origins import TrashOriginsStoreUnusableError
from tests.conftest import build_library, make_test_handle, origins_for


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


def test_trash_album_folder_takes_whole_folder_incl_sidecars(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    src_folder = album_folder(duplicates_lib, list(album.items()))
    # An untracked lyric sidecar next to the tracks — beets has no idea it exists,
    # so the per-item trash would orphan it. The whole-folder move must take it.
    (Path(src_folder) / "01 Track.lrc").write_text("[00:01.00] la", encoding="utf-8")

    with duplicates_lib.transaction():
        dest = trash_album_folder(
            duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash)
        )

    assert duplicates_lib.get_album(album_id) is None  # dropped from the library
    assert str(trash) in dest
    assert os.path.isdir(dest)
    assert (Path(dest) / "01 Track.lrc").is_file()  # the sidecar came along
    assert not os.path.exists(src_folder)  # no orphaned husk left behind


def test_trash_album_folder_ghost_folder_already_gone_drops_rows(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    src_folder = album_folder(duplicates_lib, list(album.items()))
    # The user deleted the album's folder on disk (e.g. over SMB); the DB rows
    # are all that's left. Trashing the "ghost" must drop the rows, not raise
    # FileNotFoundError trying to relocate a folder that no longer exists.
    shutil.rmtree(src_folder)

    with duplicates_lib.transaction():
        dest = trash_album_folder(
            duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash)
        )

    assert duplicates_lib.get_album(album_id) is None  # ghost rows dropped
    assert str(trash) in dest  # returns the trash dir, nothing actually moved
    assert not trash.exists()  # nothing relocated — there was nothing on disk


def test_folder_shared_guard(duplicates_lib: Library) -> None:
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_root = _album_root(duplicates_lib, list(album.items()))
    music_dir = os.path.normpath(_abs_path(duplicates_lib, duplicates_lib.directory))
    # The album's own folder is exclusively its own -> safe to move wholesale.
    assert _folder_is_shared(duplicates_lib, album, album_root) is False
    # The library root itself must NEVER be wholesale-moved.
    assert _folder_is_shared(duplicates_lib, album, music_dir) is True


def test_folder_shared_guard_survives_a_weird_path_row_stored_as_text(
    duplicates_lib: Library,
) -> None:
    """The weird-path arm reads a raw ``path`` row, and SQLite hands back its type.

    The column is declared BLOB and beets always writes bytes, but a row an
    external tool or a hand-run ``UPDATE`` wrote comes back as ``str`` — and
    ``bytes(str)`` raises ``TypeError: string argument without an encoding``. It
    escaped as a 500 from a delete that should have taken its ordinary answer.

    Reaching that arm needs a row the SCOPED query cannot match but that still
    normalizes into the folder, which is exactly what the arm exists for: a
    ``/./`` segment makes the stored prefix differ from the real one. Both halves
    are asserted — that it does not raise, AND that it still answers True — so a
    fix that skipped the row instead of decoding it would not pass.
    """
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_root = _album_root(duplicates_lib, list(album.items()))
    stranger = next(a for a in duplicates_lib.albums() if a.albumartist == "Boards of Canada")
    stranger_item = next(iter(stranger.items()))
    head, tail = album_root.rsplit(os.sep, 1)
    weird = f"{head}{os.sep}.{os.sep}{tail}{os.sep}01 Track 1.mp3"

    with duplicates_lib.transaction() as tx:
        tx.mutate("UPDATE items SET path = ? WHERE id = ?", (weird, stranger_item.id))
        typed = tx.query("SELECT typeof(path) FROM items WHERE id = ?", (stranger_item.id,))
    assert typed[0][0] == "text", "the row under test must be TEXT, not BLOB"

    # A stranger's file normalizes into this album's folder, so moving the folder
    # wholesale would take it too.
    assert _folder_is_shared(duplicates_lib, album, album_root) is True


# ----- I23: _folder_is_shared must be a scoped query, not a library scan -----


def _album_and_root(lib: Library, artist: str) -> tuple[object, str]:
    album = next(a for a in lib.albums() if a.albumartist == artist)
    return album, _album_root(lib, list(album.items()))


def test_folder_shared_never_materializes_the_item_table(
    duplicates_lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The perf property itself: the old code ran lib.items() — constructing a
    # full beets Item for every track in the library, once per trashed album
    # (minutes for delete-artist at 75k tracks, under the swap lock). The check
    # must answer from scoped SQL instead.
    calls = {"n": 0}
    real_items = Library.items

    def counting_items(
        self: Library, query: str | Sequence[str] | Query | None = None, sort: Sort | None = None
    ) -> object:
        calls["n"] += 1
        return real_items(self, query, sort)

    # Resolve album + root BEFORE arming the spy — Album.items() delegates to
    # Library.items internally, and only _folder_is_shared itself is under test.
    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    monkeypatch.setattr(Library, "items", counting_items)
    assert _folder_is_shared(duplicates_lib, album, root) is False
    assert calls["n"] == 0  # no whole-library Item materialization


def test_folder_shared_detects_a_sibling_album_in_the_same_dir(
    duplicates_lib: Library,
) -> None:
    # DATA-LOSS GUARD: two albums with files in ONE dir -> True, else trashing
    # either album's "folder" would take the other's files with it.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = _os.fsencode(_os.path.join(root, "99 Stray.mp3"))
    duplicates_lib.add_album([intruder])
    assert _folder_is_shared(duplicates_lib, album, root) is True


def test_folder_shared_detects_a_sibling_in_a_subfolder(duplicates_lib: Library) -> None:
    # A sibling parked in a SUBFOLDER of the root (e.g. a stray "Disc 2") is
    # collateral all the same.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = _os.fsencode(_os.path.join(root, "Disc 9", "01 Stray.mp3"))
    duplicates_lib.add_album([intruder])
    assert _folder_is_shared(duplicates_lib, album, root) is True


def test_folder_shared_ignores_a_string_prefix_neighbour(duplicates_lib: Library) -> None:
    # ".../Discovery" vs ".../Discovery Deluxe": a byte-prefix match without the
    # separator boundary would flag the neighbour as a sharer (false True is the
    # safe direction, but it would wrongly downgrade every whole-folder trash).
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    neighbour = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    neighbour.path = _os.fsencode(root + " Deluxe" + _os.sep + "01.mp3")
    duplicates_lib.add_album([neighbour])
    assert _folder_is_shared(duplicates_lib, album, root) is False


def test_folder_shared_matches_an_absolute_stored_row(duplicates_lib: Library) -> None:
    # beets stores paths RELATIVE to the library dir in the normal case, but
    # legacy rows can be ABSOLUTE. Both stored forms must be matched — missing
    # the absolute arm would return a false "not shared" for such a row.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = _os.fsencode(_os.path.join(root, "98 Legacy.mp3"))
    duplicates_lib.add_album([intruder])
    with duplicates_lib.transaction() as tx:
        tx.mutate(
            "UPDATE items SET path = ? WHERE id = ?",
            (_os.fsencode(_os.path.join(root, "98 Legacy.mp3")), _require_id(intruder.id)),
        )
    assert _folder_is_shared(duplicates_lib, album, root) is True


def test_folder_shared_weird_path_row_falls_back_to_full_normalization(
    duplicates_lib: Library,
) -> None:
    # DATA-LOSS GUARD for the byte-prefix blind spot: a stored path containing a
    # ".." segment normalizes UNDER the root while its bytes never prefix-match
    # it. The old scan caught this via per-item normpath; the scoped query must
    # route such rows through the same normalization rather than trusting bytes.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = _os.fsencode(_os.path.join(root, "97 Weird.mp3"))
    duplicates_lib.add_album([intruder])
    music_dir = _os.fsdecode(duplicates_lib.directory)
    rel_weird = _os.path.join("Elsewhere", "..", _os.path.relpath(root, music_dir), "97 Weird.mp3")
    with duplicates_lib.transaction() as tx:
        tx.mutate(
            "UPDATE items SET path = ? WHERE id = ?",
            (_os.fsencode(rel_weird), _require_id(intruder.id)),
        )
    assert _folder_is_shared(duplicates_lib, album, root) is True


def test_folder_shared_counts_a_singleton_under_the_root(duplicates_lib: Library) -> None:
    # A singleton item (album_id NULL) parked in the folder is collateral too —
    # the old loop treated it as a sharer via `int(item.album_id or 0)`.
    import os as _os

    from beets.library import Item

    album, root = _album_and_root(duplicates_lib, "Daft Punk")
    single = Item(album="", albumartist="", artist="Solo", title="loose", track=0)
    single.path = _os.fsencode(_os.path.join(root, "96 Loose.mp3"))
    duplicates_lib.add(single)  # item WITHOUT an album row
    assert _folder_is_shared(duplicates_lib, album, root) is True


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
# Both row-dropping primitives here (``trash_album_folder``'s ghost branch and
# ``trash_album``, whose per-item ``Album.move`` SILENTLY skips missing sources —
# beets 2.12 library/models.py:1178-1192) read an absent path as "the user deleted
# this". With the library root itself gone that reading is wrong for EVERY album,
# so the shared root predicate has to run before the first mutation. It matters
# doubly because a beets ``Transaction`` COMMITS on the way out even when it is
# unwinding an exception (dbcore/db.py:924-941, no rollback branch): anything
# dropped before the raise would stick.


def test_trash_album_folder_root_gone_raises_and_keeps_rows(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """Root missing entirely (share unmounted, mountpoint removed) -> raise, drop nothing."""
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))  # the WHOLE music root

    with pytest.raises(LibraryRootUnavailableError), duplicates_lib.transaction():
        trash_album_folder(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert duplicates_lib.get_album(album_id) is not None  # rows survive the commit-on-exit
    assert not trash.exists()  # nothing relocated either


def test_trash_album_folder_root_present_but_empty_raises(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The dropped-NAS signature: the mountpoint dir stays, its contents vanish.

    ``os.path.isdir`` is still True here, so an isdir-only guard would wave this
    through and the ghost branch would drop the rows. Pins that the FULL shared
    predicate (missing OR empty OR unreadable) is what runs — not a parallel
    re-implementation.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    root = os.fsdecode(duplicates_lib.directory)
    shutil.rmtree(root)
    os.mkdir(root)  # present but empty
    assert os.path.isdir(root)

    with pytest.raises(LibraryRootUnavailableError), duplicates_lib.transaction():
        trash_album_folder(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

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

    with pytest.raises(LibraryRootUnavailableError), duplicates_lib.transaction():
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

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

    req = _StubRequest(_StubApp(make_test_handle(duplicates_lib, tmp_path)))
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

    with pytest.raises(LibraryRootUnavailableError) as ei, duplicates_lib.transaction():
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert "unreadable" in str(ei.value)
    assert "empty" not in str(ei.value)
    assert "Permission denied" in str(ei.value)  # the OS's own reason, no path
    assert duplicates_lib.get_album(album_id) is not None  # still fails CLOSED
    assert not trash.exists()


# ----- The guard is a PRE-check; the move needs its own POST-condition -----
#
# Passing the root check does not make the moves happen. The window between the
# check and ``Album.move`` is enough for a share to drop, and beets answers a
# missing source by logging and returning (models.py:1178-1192) — so ``move``
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

    with pytest.raises(LibraryRootUnavailableError), duplicates_lib.transaction():
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert duplicates_lib.get_album(album_id) is not None  # rows kept
    assert list(trash.iterdir()) == []  # and no phantom empty container in Trash


def test_trash_album_folder_fallback_inherits_the_post_condition(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared-folder fallback reaches the same primitive, so the same holds.

    This is the shape the deep review drove through ``delete_artist``: a folder
    another album has a file in cannot be moved wholesale, so it falls back to
    the per-item ``trash_album`` — where the vanish window lives.
    """
    from beets.library import Item

    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    album_root = _album_root(duplicates_lib, list(album.items()))
    intruder = Item(album="Other", albumartist="Other", artist="Other", title="x", track=1)
    intruder.path = os.fsencode(os.path.join(album_root, "99 Not Mine.mp3"))
    duplicates_lib.add_album([intruder])  # now the folder is shared
    root = os.fsdecode(duplicates_lib.directory)
    real_guard = require_library_root  # read from its own module; patched on trash_mod
    calls = {"n": 0}

    def _drops_the_instant_it_passes(lib: Library) -> None:
        calls["n"] += 1
        real_guard(lib)
        if calls["n"] == 1:
            shutil.rmtree(root)

    monkeypatch.setattr(trash_mod, "require_library_root", _drops_the_instant_it_passes)

    with pytest.raises(LibraryRootUnavailableError), duplicates_lib.transaction():
        trash_album_folder(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert duplicates_lib.get_album(album_id) is not None
    assert list(trash.iterdir()) == []


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

    with pytest.raises(TrashMoveIncompleteError) as ei, duplicates_lib.transaction():
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

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
    gone. Import Replace and duplicates resolve reach ``trash_album`` directly
    (no ghost branch in front of them, unlike ``trash_album_folder``), and both
    rely on the rows being dropped here. Refusing would strand every replaced
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


def test_trash_album_folder_refuses_a_dropped_share_masked_by_a_stray(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The whole-folder ghost branch: the folder is missing because the SHARE is."""
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    total_before = len(list(duplicates_lib.albums()))
    _mountpoint_with_only_a_stray(duplicates_lib)
    require_library_root(duplicates_lib)  # the cheap predicate is happy — the bug

    with pytest.raises(LibraryRootUnavailableError), duplicates_lib.transaction():
        trash_album_folder(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert duplicates_lib.get_album(album_id) is not None  # rows kept
    assert len(list(duplicates_lib.albums())) == total_before
    assert not trash.exists()


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

    with pytest.raises(LibraryRootUnavailableError), duplicates_lib.transaction():
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert duplicates_lib.get_album(album_id) is not None  # rows kept
    assert list(trash.iterdir()) == []  # no phantom container left behind


def test_delete_artist_fan_out_stops_on_the_first_masked_drop(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The fan-out must not erase album after album through the masked drop.

    Radiohead holds two albums; a guard that fired only after the first
    ``album.remove`` would leave one row committed and unrecoverable (beets
    commits on the way out of the transaction even while unwinding).
    """
    from app.beets.delete import delete_artist

    trash = tmp_path / "trash"
    total_before = len(list(duplicates_lib.albums()))
    _mountpoint_with_only_a_stray(duplicates_lib, "lost+found")

    with pytest.raises(LibraryRootUnavailableError):
        delete_artist(duplicates_lib, "Radiohead", trash_dir=trash, origins_dir=origins_for(trash))

    assert len(list(duplicates_lib.albums())) == total_before
    assert not trash.exists()


# ----- The same dropped share, under a FLAT path template -----
#
# A ``paths.default`` with no directory component (beets' own ``$title``,
# editable from Settings -> Naming) files every track directly in the music
# root, so the delete path reaches this state by a DIFFERENT arm than the two
# tests above: ``_album_root`` is the music root, which exists, and it is shared
# with every other album, so ``trash_album_folder`` falls through to the
# per-item ``trash_album`` and the drop would happen in
# ``_require_move_happened``'s ghost arm. Measured on the folder-based sampler:
# 20 album rows became 19 with Trash empty. A test that only called
# ``require_library_present`` would not prove the arm that actually fires is
# guarded.


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


def test_trash_album_folder_refuses_a_dropped_flat_share_masked_by_a_stray(
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

    with pytest.raises(LibraryRootUnavailableError), lib.transaction():
        trash_album_folder(lib, album, trash_dir=trash, origins_dir=origins_for(trash))

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

    with pytest.raises(TrashOriginsStoreUnusableError) as ei, duplicates_lib.transaction():
        trash_album(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert "trash-origins" in str(ei.value)
    assert duplicates_lib.get_album(album_id) is not None
    assert os.path.isdir(folder), "the album's files must still be where they were"
    assert not trash.exists(), "the refusal comes before the Trash mkdir"


def test_trash_album_folder_refuses_an_unusable_store_before_the_GHOST_branch(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The check sits above every branch, including the two that move nothing.

    A ghost album — rows with no folder — is dropped by a branch that relocates
    not one byte, so a check placed beside the ``mkdir`` would let it through.
    The invariant the owner ruled on is about ROWS, not about files: a fan-out
    that refused its third album having already erased two ghosts would have
    broken it while every file stayed exactly where it was.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    shutil.rmtree(album_folder(duplicates_lib, list(album.items())))  # ghost: rows only
    _file_at_the_store(tmp_path)

    with pytest.raises(TrashOriginsStoreUnusableError), duplicates_lib.transaction():
        trash_album_folder(duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash))

    assert duplicates_lib.get_album(album_id) is not None, "the ghost rows must survive"
    assert not trash.exists()


def test_a_store_that_does_not_exist_yet_is_created_by_the_delete(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """Absence is the FRESH-INSTALL shape and must not be read as a fault.

    No deployment has an origins directory until its first delete. A guard that
    treated ENOENT like EACCES would refuse every first delete there is, so this
    is the counter-example the check is written against — end to end through the
    mover, not only at the predicate.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    assert not origins_for(trash).exists(), "the fixture must start with no store"

    with duplicates_lib.transaction():
        dest = trash_album_folder(
            duplicates_lib, album, trash_dir=trash, origins_dir=origins_for(trash)
        )

    assert duplicates_lib.get_album(album_id) is None, "the delete must have completed"
    assert origins_for(trash).is_dir()
    assert (origins_for(trash) / f"{Path(dest).name}.json").is_file()
