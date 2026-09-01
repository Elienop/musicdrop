"""Tests for the reversible delete (move-to-Trash) of albums and artists."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from beets.library import Library
from fastapi import HTTPException

from app.beets import delete as delete_mod
from app.beets.delete import (
    AlbumNotFoundError,
    delete_album,
    delete_album_op,
    delete_artist,
    delete_artist_op,
)
from app.beets.library import LibraryRootUnavailableError, _require_id
from app.beets.trash import album_folder, trash_album_folder
from app.config import Settings
from tests.conftest import make_test_handle, origins_for


def test_delete_album_trashes_whole_folder_and_drops(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = album_folder(duplicates_lib, list(album.items()))
    (Path(folder) / "cover-extra.lrc").write_text("[00:01.00] x", encoding="utf-8")

    result = delete_album(duplicates_lib, album_id, trash_dir=trash, origins_dir=origins_for(trash))

    assert result.trashed_albums == 1
    assert str(trash) in result.trash_path
    assert duplicates_lib.get_album(album_id) is None  # dropped from the library
    assert (Path(result.trash_path) / "cover-extra.lrc").is_file()  # sidecar came along
    assert not os.path.exists(folder)  # no orphaned husk


def test_delete_album_ghost_folder_already_gone(duplicates_lib: Library, tmp_path: Path) -> None:
    """Folder deleted on disk outside MusicDrop -> drop the ghost rows, don't 500.

    Reproduces the live user case: an artist folder removed over SMB leaves the
    beets DB rows behind. The front-door delete must clean them, not fail on the
    missing source folder.
    """
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = album_folder(duplicates_lib, list(album.items()))
    shutil.rmtree(folder)  # ghost: DB rows remain, the files are gone

    result = delete_album(duplicates_lib, album_id, trash_dir=trash, origins_dir=origins_for(trash))

    assert result.trashed_albums == 1
    assert duplicates_lib.get_album(album_id) is None  # ghost rows dropped
    assert not trash.exists()  # nothing relocated — there was nothing to move


def test_delete_album_unknown_id_raises(duplicates_lib: Library, tmp_path: Path) -> None:
    with pytest.raises(AlbumNotFoundError):
        delete_album(
            duplicates_lib,
            999_999,
            trash_dir=tmp_path / "trash",
            origins_dir=tmp_path / "trash-origins",
        )


def test_delete_artist_trashes_all_their_albums(duplicates_lib: Library, tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    before = [a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk"]
    assert before, "fixture should have at least one Daft Punk album"

    result = delete_artist(
        duplicates_lib, "Daft Punk", trash_dir=trash, origins_dir=origins_for(trash)
    )

    assert result.trashed_albums == len(before)
    assert not [a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk"]


def test_delete_artist_unknown_is_noop(duplicates_lib: Library, tmp_path: Path) -> None:
    result = delete_artist(
        duplicates_lib,
        "Nobody At All",
        trash_dir=tmp_path / "trash",
        origins_dir=tmp_path / "trash-origins",
    )
    assert result.trashed_albums == 0


def test_delete_album_op_409_during_backfill() -> None:
    """A running library job blocks delete with 409 — before touching the lib."""
    from app.lyrics_jobs.registry import reset_lyrics_backfill

    reset_lyrics_backfill().start(writes_enabled=True)

    class _App:
        class state:
            beets_library = None

    class _Req:
        app = _App()

    try:
        req = _Req()
        # Calling the async op only CREATES the coroutine — nothing runs (and
        # nothing can raise) until asyncio.run drives it inside the block.
        op = delete_album_op(req, 1)  # type: ignore[arg-type]
        with pytest.raises(HTTPException) as ei:
            asyncio.run(op)
        assert ei.value.status_code == 409
    finally:
        reset_lyrics_backfill()


# ----- The unmounted-share guard, from the front door -----
#
# Both delete entry points ride ``trash_album_folder``, whose ghost branch reads
# a missing album folder as "deleted outside MusicDrop". With the library root
# itself gone that reading is wrong for every album at once, so the primitive's
# root guard has to fire before the first row drop — and the drop it prevents
# would otherwise be permanent, because a beets ``Transaction`` commits on the
# way out even while unwinding an exception (dbcore/db.py:924-941).


def test_delete_album_root_unavailable_keeps_rows(duplicates_lib: Library, tmp_path: Path) -> None:
    """No DeleteResult(trashed_albums=1) lie: the op raises and the album stays."""
    trash = tmp_path / "trash"
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    with pytest.raises(LibraryRootUnavailableError):
        delete_album(duplicates_lib, album_id, trash_dir=trash, origins_dir=origins_for(trash))

    assert duplicates_lib.get_album(album_id) is not None  # still queryable
    assert not trash.exists()


def test_delete_artist_root_unavailable_drops_nothing(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The fan-out aborts on the FIRST album, before any row is dropped.

    Radiohead holds two albums in the fixture; a guard that ran anywhere after
    the first ``album.remove`` would leave one row committed and unrecoverable.
    """
    trash = tmp_path / "trash"
    before = [_require_id(a.id) for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]
    assert len(before) == 2, "fixture should hold two Radiohead albums"
    total_before = len(list(duplicates_lib.albums()))
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    with pytest.raises(LibraryRootUnavailableError):
        delete_artist(duplicates_lib, "Radiohead", trash_dir=trash, origins_dir=origins_for(trash))

    assert [_require_id(a.id) for a in duplicates_lib.albums() if a.albumartist == "Radiohead"] == (
        before
    )
    assert len(list(duplicates_lib.albums())) == total_before
    assert not trash.exists()


def test_delete_album_op_503_root_unavailable(duplicates_lib: Library, tmp_path: Path) -> None:
    """The HTTP mapping: 503 + the flat honest sentence, not the structured 500.

    The blanket ``except`` above would answer "Files are recoverable in the Trash
    folder" for an operation that moved nothing, so the root-unavailable arm has
    to sit in front of it. The stub request is enough because ``_swap_lock``
    creates its lock lazily on whatever ``app.state`` it is handed
    (config_editor.py:624-630) and ``_settings`` falls back to the module
    singleton when ``state.settings`` is absent (:632-645).
    """
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    handle = make_test_handle(duplicates_lib, tmp_path)
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    class _App:
        state = SimpleNamespace(beets_library=handle)

    class _Req:
        app = _App()

    req = _Req()
    coro = delete_album_op(req, album_id)  # type: ignore[arg-type]  # duck-typed stub
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 503
    assert ei.value.detail == "Library folder unavailable. Is the music share mounted?"
    assert duplicates_lib.get_album(album_id) is not None


def test_delete_artist_op_503_root_unavailable(duplicates_lib: Library, tmp_path: Path) -> None:
    """The artist op maps the same cause the same way — its own arm, its own pin.

    Without this the artist half of the mapping is unpinned: the route-status
    census reads the RAISE and only flags a status raised-but-undeclared, so
    deleting the ``except`` here would leave a declared 503 nothing produces and
    hand the fan-out back its "recoverable in the Trash folder" 500.
    """
    handle = make_test_handle(duplicates_lib, tmp_path)
    before = len(list(duplicates_lib.albums()))
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    class _App:
        state = SimpleNamespace(beets_library=handle)

    class _Req:
        app = _App()

    req = _Req()
    coro = delete_artist_op(req, "Radiohead")  # type: ignore[arg-type]  # duck-typed stub
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 503
    assert ei.value.detail == "Library folder unavailable. Is the music share mounted?"
    assert len(list(duplicates_lib.albums())) == before


def test_delete_album_op_503_masked_drop_says_what_to_do(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The wire answer for a drop the ROOT check cannot see.

    A ``.stfolder`` on the local mountpoint keeps ``require_library_root``
    passing, so without the stronger predicate this request would answer
    ``200 trashed_albums=1`` having moved nothing and dropped the rows. It must
    be a 503 whose sentence tells the user what to check — and which names no
    filesystem path, because the detail is rendered straight into the dialog.
    """
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    handle = make_test_handle(duplicates_lib, tmp_path)
    root = Path(os.fsdecode(duplicates_lib.directory))
    shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / ".stfolder").mkdir()

    class _App:
        state = SimpleNamespace(beets_library=handle)

    class _Req:
        app = _App()

    req = _Req()
    coro = delete_album_op(req, album_id)  # type: ignore[arg-type]  # duck-typed stub
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 503
    assert ei.value.detail == (
        "Library folder is present but holds none of the library's albums."
        " Either the music share is not mounted, or every album's folder has been"
        " removed outside MusicDrop."
    )
    assert duplicates_lib.get_album(album_id) is not None  # rows kept
    assert str(root) not in str(ei.value.detail)


def test_delete_artist_root_gone_raises_before_the_transaction(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ENTRY check is what makes the artist 503's "nothing was dropped" true.

    Relying on the primitive alone would leave that claim conditional: the
    primitive only checks the root once it is already inside the transaction,
    per album. Pinned by stubbing the primitive out entirely — with nothing else
    in the fan-out able to raise, a raise here can only have come from the
    check that runs before the loop.
    """
    calls: list[int] = []

    def _never(*args: object, **kwargs: object) -> str:
        calls.append(1)
        return ""

    monkeypatch.setattr(delete_mod, "trash_album_folder", _never)
    before = len(list(duplicates_lib.albums()))
    shutil.rmtree(os.fsdecode(duplicates_lib.directory))

    with pytest.raises(LibraryRootUnavailableError):
        delete_artist(
            duplicates_lib,
            "Radiohead",
            trash_dir=tmp_path / "trash",
            origins_dir=tmp_path / "trash-origins",
        )

    assert calls == []  # the fan-out never started
    assert len(list(duplicates_lib.albums())) == before


def test_delete_artist_op_reports_partial_progress_for_ANY_cause(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same two tiers, for a cause that is not the unmounted share.

    The test below pins this for ``LibraryRootUnavailableError``, and until now
    that was the ONLY cause that got it: a permission error, a full disk or a DB
    fault mid-fan-out reached the user as a bare message with no count, while
    beets had already committed every album before it. Same state, strictly
    worse reporting, and nothing said which.

    ``PermissionError`` here stands for the whole class -- what matters is that
    it is not the one exception type the arm above already names.
    """
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, tmp_path)
    real = trash_album_folder
    calls = {"n": 0}

    def _fails_on_the_second(
        lib: Library, album: object, *, trash_dir: Path, origins_dir: Path
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(13, "Permission denied")
        return str(real(lib, album, trash_dir=trash_dir, origins_dir=origins_dir))

    monkeypatch.setattr(delete_mod, "trash_album_folder", _fails_on_the_second)

    class _App:
        state = SimpleNamespace(beets_library=handle, settings=Settings(trash_dir=str(trash)))

    class _Req:
        app = _App()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_artist_op(_Req(), "Radiohead"))  # type: ignore[arg-type]

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "1 of 2" in detail["message"]  # the count survives, which it did not before
    assert "Permission denied" in detail["message"]  # and the real cause is still named


def test_delete_artist_op_mid_flight_drop_reports_partial_progress(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share that drops MID fan-out must not answer with the 503.

    The 503 (and its OpenAPI description) says nothing was moved or dropped.
    Once an album has been through the primitive that is false and unfixable —
    beets commits on the way out of the transaction even while unwinding the
    exception — so this case has to surface as the structured 500 whose message
    states how far the fan-out got.
    """
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, tmp_path)
    real = trash_album_folder  # from its own module: delete.py does not re-export it
    calls = {"n": 0}

    def _drops_on_the_second(
        lib: Library, album: object, *, trash_dir: Path, origins_dir: Path
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise LibraryRootUnavailableError(
                "Library folder unavailable. Is the music share mounted?"
            )
        return str(real(lib, album, trash_dir=trash_dir, origins_dir=origins_dir))

    monkeypatch.setattr(delete_mod, "trash_album_folder", _drops_on_the_second)

    class _App:
        # An explicit Settings pins the Trash location inside tmp_path: this is
        # the one op test that really moves files, and _settings would otherwise
        # fall back to the module singleton (a dev .env could point anywhere).
        state = SimpleNamespace(beets_library=handle, settings=Settings(trash_dir=str(trash)))

    class _Req:
        app = _App()

    req = _Req()
    coro = delete_artist_op(req, "Radiohead")  # type: ignore[arg-type]  # duck-typed stub
    with pytest.raises(HTTPException) as ei:
        asyncio.run(coro)

    assert ei.value.status_code == 500  # NOT the nothing-was-dropped 503
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "1 of 2" in detail["message"]  # names how far it got
    assert "music share" in detail["message"]  # and why it stopped
    # The library agrees with the message: one album trashed, one untouched.
    assert len([a for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]) == 1
    assert trash.is_dir()


def test_delete_album_op_records_an_origin_the_listing_can_offer_a_move_back_on(
    duplicates_lib: Library, tmp_path: Path
) -> None:
    """The op resolves BOTH dirs, and only an end-to-end check sees the second.

    Every unit test below the op passes ``origins_dir`` explicitly, so an op that
    resolved it wrong — as the Trash dir, say, which would put the records inside
    the entry namespace the listing walks — is invisible to all of them and
    silently degrades every deleted album to an import-restore.
    """
    from app.beets.trash_manage import list_trashed_albums

    trash = tmp_path / "trash"
    origins = tmp_path / "trash-origins"
    handle = make_test_handle(duplicates_lib, tmp_path)
    album = next(iter(duplicates_lib.albums()))
    album_id = _require_id(album.id)
    album_root = os.path.dirname(os.fsdecode(next(iter(album.items())).path))

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins)),
        )

    class _Req:
        app = _App()

    asyncio.run(delete_album_op(_Req(), album_id))  # type: ignore[arg-type]  # duck-typed stub

    assert list(origins.glob("*.json")), "the op resolved no origin store"
    assert not list(trash.glob("*.json")), "records must not land in the entry namespace"
    rows = list_trashed_albums(
        trash, origins_dir=origins, music_dir=os.fsdecode(duplicates_lib.directory)
    )
    (row,) = [r for r in rows if r.origin == album_root]
    assert row.restore_mode == "move_back"
