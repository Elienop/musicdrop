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
from tests.conftest import build_library, make_test_handle, origins_for


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


# --- the partial-failure message may only claim what really happened ---------
# The fan-out's 500 is read by someone whose delete half-finished, and it used
# to tell them two things it could not know: that "the music share became
# unavailable" (the guard that refuses says it is EITHER that or an artist whose
# folders were removed outside MusicDrop, and cannot tell which), and that N
# albums are waiting in Trash (the primitive drops the rows of a ghost or an
# empty album having moved nothing at all).


_GHOST_ARTIST = "Ghosty"


def _ghost_artist_library(tmp_path: Path, *, real_album: bool, bystander: bool) -> Library:
    """A library whose ``Ghosty`` albums are ROWS ONLY — their folders are gone.

    Built here rather than from ``duplicates_lib`` because the shape under test
    is a specific one: albums whose folders were removed outside MusicDrop while
    the share is perfectly fine.

    Two knobs, and each is what makes one scenario DETERMINISTIC rather than a
    coin flip on ``require_library_present``'s random sample:

    * ``real_album`` adds one Ghosty album that is really on disk and sorts
      FIRST (beets orders ``lib.albums()`` by ``albumartist+ album+``), so the
      fan-out moves it and then meets a library whose every remaining album is a
      ghost — the refusal is then certain, and it lands with progress already
      made;
    * ``bystander`` adds one album by another artist that is really on disk, so
      the refusal never happens at all: the sample size is 5 and this library
      holds 5 albums or fewer, which makes every draw exhaustive and therefore
      always a hit.

    An unrelated folder of real files sits in the music root either way. It is
    what makes "the share is mounted" a fact of the fixture rather than an
    assumption: the guard samples only albums the LIBRARY knows about, so this
    folder cannot make it pass, and any test can stat it at the end.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, on_disk: bool) -> None:
        base = music / artist / album
        base.mkdir(parents=True, exist_ok=True)
        f = base / "01 Track.mp3"
        f.write_bytes(b"\x00")
        item = Item(album=album, albumartist=artist, artist=artist, title="Track", track=1)
        item.path = os.fsencode(str(f))
        lib.add_album([item]).store()
        if not on_disk:
            shutil.rmtree(base)  # removed outside MusicDrop; the rows survive

    if real_album:
        # Sorts before every "Ghost N" under the same albumartist.
        add(artist=_GHOST_ARTIST, album="A Real Album", on_disk=True)
    for n in range(1, 5):
        add(artist=_GHOST_ARTIST, album=f"Ghost {n}", on_disk=False)
    if bystander:
        add(artist="Bystander", album="Still Here", on_disk=True)

    proof = music / "Not In The Library" / "sleeve.jpg"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_bytes(b"\x00")
    return lib


def _artist_op_500(lib: Library, tmp_path: Path, trash: Path) -> HTTPException:
    """Run ``delete_artist_op`` for the ghost artist and return the 500 it raises."""
    handle = make_test_handle(lib, tmp_path)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_artist_op(_Req(), _GHOST_ARTIST))  # type: ignore[arg-type]  # stub req
    assert ei.value.status_code == 500
    return ei.value


def test_delete_artist_partial_does_not_diagnose_an_unmounted_share(tmp_path: Path) -> None:
    """A refusal this fan-out CANNOT explain must be relayed, not diagnosed.

    ``require_library_present`` refuses when none of the albums it sampled is on
    disk, and its own message names two causes because it cannot tell them
    apart. This artist's folders were removed outside MusicDrop and the share is
    fine — provable here, since the fixture's unrelated folder is readable
    throughout — so "the music share became unavailable" was a false statement
    of fact printed to the one user who would act on it.
    """
    lib = _ghost_artist_library(tmp_path, real_album=True, bystander=False)
    trash = tmp_path / "trash"
    ghosty = [a.album for a in lib.albums() if a.albumartist == _GHOST_ARTIST]
    assert ghosty[0] == "A Real Album", "the fan-out must reach the real album first"
    assert len(ghosty) == 5

    detail = _artist_op_500(lib, tmp_path, trash).detail

    assert isinstance(detail, dict)
    message = detail["message"]
    assert "1 of 5" in message  # how far it got, unchanged
    assert "became unavailable" not in message  # the cause it was never told
    # ...and the words the error DID use, both of its causes intact.
    assert "Either the music share is not mounted, or every album's folder has been" in message
    # The share really was mounted the whole time: this file never stopped being
    # readable, so a message blaming the mount would have been false, not unlucky.
    assert (tmp_path / "music" / "Not In The Library" / "sleeve.jpg").is_file()
    # One album's files really are in Trash, so that half of the promise stands.
    assert (trash / "A Real Album" / "01 Track.mp3").is_file()
    assert detail["recovery"] == "Files are recoverable in the Trash folder. Retry."


def test_delete_artist_partial_counts_moves_not_row_drops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ghost rows are dropped, not trashed — so the message may not say "moved".

    Every album here is a ghost, so the fan-out cleans rows and relocates
    nothing; the bystander is what makes the presence guard pass every time
    rather than at random. The old message offered "3 of 4 albums had been moved
    to Trash" and the 500 offered to find them there, for a Trash folder that
    does not exist.
    """
    lib = _ghost_artist_library(tmp_path, real_album=False, bystander=True)
    trash = tmp_path / "trash"
    real = trash_album_folder
    calls = {"n": 0}

    def _fails_on_the_fourth(
        lib: Library, album: object, *, trash_dir: Path, origins_dir: Path
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 4:
            raise PermissionError(13, "Permission denied")
        return str(real(lib, album, trash_dir=trash_dir, origins_dir=origins_dir))

    monkeypatch.setattr(delete_mod, "trash_album_folder", _fails_on_the_fourth)

    detail = _artist_op_500(lib, tmp_path, trash).detail

    assert isinstance(detail, dict)
    message = detail["message"]
    assert "3 of 4" in message  # three albums' rows are gone, and it says so
    assert "moved to Trash" not in message  # but not one file was
    assert "Permission denied" in message  # the real cause still names itself
    # The physical fact the wording has to match: there is no Trash folder at
    # all, so a line telling the user to look in it points at nothing.
    assert not trash.exists()
    assert detail["recovery"] == (
        "Nothing reached the Trash folder, so there is nothing to restore. Retry."
    )
    assert len([a for a in lib.albums() if a.albumartist == _GHOST_ARTIST]) == 1


def test_delete_album_500_does_not_promise_trash_for_files_that_did_not_move(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TrashMoveIncompleteError`` is raised BECAUSE nothing moved.

    Its own message ends "The library rows were kept", and the 500 wrapping it
    answered "Files are recoverable in the Trash folder" — a contradiction
    inside one body, about files still sitting in the music library.
    """
    from app.beets.trash import TrashMoveIncompleteError

    def _refuses(*args: object, **kwargs: object) -> str:
        raise TrashMoveIncompleteError("'X' did not move to Trash. The library rows were kept.")

    monkeypatch.setattr(delete_mod, "trash_album_folder", _refuses)
    handle = make_test_handle(duplicates_lib, tmp_path)
    album_id = _require_id(next(iter(duplicates_lib.albums())).id)

    class _App:
        state = SimpleNamespace(beets_library=handle, settings=Settings())

    class _Req:
        app = _App()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_album_op(_Req(), album_id))  # type: ignore[arg-type]  # stub req

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "recoverable in the Trash folder" not in detail["recovery"]
    assert detail["recovery"] == (
        "The files were not moved and the library still has the album. Retry."
    )


def test_delete_artist_500_on_the_FIRST_album_does_not_promise_trash(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fan-out that fails on album 1 of 2 has moved nothing — and must say so.

    The tests above cover the fan-out that got PAST its first album: it raises
    ``ArtistDeletePartialError``, which carries the count the recovery line is
    chosen from. A failure on the FIRST album never reaches that helper —
    ``mutated == 0`` re-raises the cause bare so the 503 tier stays available —
    so the 500 wrapping it took the blanket "Files are recoverable in the Trash
    folder" while not one file had moved and the Trash folder did not exist. The
    same wrong sentence as the ghost fan-out's, down the one path nothing
    enumerated.

    Pinned on the physical fact as well as the wording: the Trash dir is never
    created, and both albums are still in the library.
    """
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, tmp_path)

    def _fails_on_the_first(
        lib: Library, album: object, *, trash_dir: Path, origins_dir: Path
    ) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(delete_mod, "trash_album_folder", _fails_on_the_first)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    before = [a for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]
    assert len(before) == 2, "'the first album' only means something with more than one"

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_artist_op(_Req(), "Radiohead"))  # type: ignore[arg-type]  # stub req

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "Permission denied" in detail["message"]  # the cause is still relayed
    assert "recoverable in the Trash folder" not in detail["recovery"]
    assert detail["recovery"] == (
        "Nothing reached the Trash folder, so there is nothing to restore. Retry."
    )
    # The disk agrees with the sentence: there is no Trash folder to look in.
    assert not trash.exists()
    assert len([a for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]) == 2
