"""Tests for the reversible delete (move-to-Trash) of albums and artists."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from beets.library import Album, Library
from fastapi import HTTPException

from app.beets import delete as delete_mod
from app.beets import trash as trash_mod
from app.beets.delete import (
    AlbumNotFoundError,
    delete_album,
    delete_album_op,
    delete_artist,
    delete_artist_op,
)
from app.beets.library import LibraryRootUnavailableError, _require_id
from app.beets.trash import album_folder, trash_album_folder
from app.beets.trash_origins import require_usable_store
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

    The blanket ``except`` above would answer with the 500's recovery line, which
    sends the reader to check a Trash folder this operation never created (and
    for most of this branch's life promised the files were already in it), so the
    root-unavailable arm has to sit in front of it. The 503 says the one thing
    that is known instead. The stub request is enough because ``_swap_lock``
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
    hand the fan-out back the blanket 500, whose recovery line can only tell the
    user to go and look in Trash.
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


# ----- the SECOND setup fault a delete refuses: an unusable origin store ------
#
# Owner ruling, ``decisions.md`` 28. Same tier and same shape as the root guard
# above, for the same reason: the check runs before anything moves or is
# dropped, so the one fact known on every path is the one the 503 states.


def _store_fault_req(lib: Library, tmp_path: Path, trash: Path) -> object:
    """A stub request whose settings point the ops at ``trash``'s sibling store."""
    handle = make_test_handle(lib, tmp_path)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    return _Req()


@pytest.mark.parametrize("artist", [None, "Radiohead"], ids=["album", "artist"])
@pytest.mark.parametrize(
    ("shape", "says"),
    [
        ("file", "is not a usable folder"),
        ("unsearchable", "cannot be read"),
        ("unreadable", "cannot be read"),
    ],
)
def test_delete_op_503_when_the_origin_store_cannot_be_used(
    duplicates_lib: Library, tmp_path: Path, artist: str | None, shape: str, says: str
) -> None:
    """Both ops, both measured shapes: a 503 that names the store and leaks no path.

    The two shapes are not a duplication. ``file`` — a regular FILE where the
    store belongs — is the one nothing could see before this guard, because
    ``Path.exists()`` absorbs the ENOTDIR its children raise, and it is
    ROOT-PROOF: it denies for uid 0 exactly as it does for uid 1000.
    ``unsearchable`` — the directory at mode 0600 — is the ruling's own headline
    shape (a bad ``PUID``/``PGID``, a restored backup) and is the one that must
    skip under root, where a mode bit denies nothing and the arm would report
    green having exercised no fault.

    Both ends of the message are asserted, because they pull in opposite
    directions: the sentence has to NAME the store (or the reader cannot act on
    it) while carrying no absolute path (the presence refusal beside it leaks
    none either — ``test_refusal_message_leaks_no_path``), and only asserting
    one of the two lets the other regress.

    ``unreadable`` — mode 0300, ``-wx`` — is the third, and it is the only shape
    the ``scandir`` probe answers by itself: measured at 0300, the ``mkdir``
    succeeds, the ``stat`` of the never-there key answers ENOENT (healthy) and
    the ``mkstemp`` write succeeds, so with the ``scandir`` dropped the whole
    guard passes and the delete runs. It is the mode a store gets when the Empty
    all sweep can no longer list it, which is the half of the probe the other
    two rows do not reach. Skips under root for the same reason as the row
    above.

    ``says`` pins WHICH of the three probes answered, and it is not decoration:
    an unsearchable store fails the write probe too, so without it the search
    probe is a line no test can kill (measured — dropping it left 59 tests
    green, with the refusal still raised and only its sentence changed from
    "cannot be read" to "cannot be written"). The two sentences send an operator
    at different permission bits.
    """
    if shape != "file" and os.getuid() == 0:
        pytest.skip("running as root: a mode bit on a directory denies nothing")
    trash = tmp_path / "trash"
    origins = origins_for(trash)
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    folder = album_folder(duplicates_lib, list(album.items()))
    total_before = len(list(duplicates_lib.albums()))
    if shape == "file":
        origins.write_bytes(b"not a directory")
    else:
        origins.mkdir()
        (origins / "Someone Elses.json").write_text("{}", encoding="utf-8")
        # 0600, not 0000: the ``unsearchable`` shape LISTS fine and only fails a
        # lookup of a child, which is the fault ``origin_recorded`` meets and the
        # only one the search probe is there for. At 0000 the ``scandir`` probe
        # answers first, and the ``stat`` becomes a line no test can kill
        # (measured). 0300 is the mirror: it cannot be LISTED and everything
        # else about it works.
        origins.chmod(0o600 if shape == "unsearchable" else 0o300)

    req = _store_fault_req(duplicates_lib, tmp_path, trash)
    op = (
        delete_artist_op(req, artist)  # type: ignore[arg-type]  # stub req
        if artist is not None
        else delete_album_op(req, album_id)  # type: ignore[arg-type]  # ditto
    )
    try:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(op)
    finally:
        if shape != "file":
            origins.chmod(0o700)

    assert ei.value.status_code == 503
    detail = ei.value.detail
    assert isinstance(detail, str), "flat, like the root guard's — not the structured 500"
    assert "trash-origins" in detail, "the reader has to be told WHICH folder to fix"
    assert says in detail, "and which of the three questions the store failed"
    assert str(origins) not in detail, "the store's absolute path must not be in it"
    assert str(tmp_path) not in detail, "...nor any prefix of it"
    # The promise sits BETWEEN the cause and the instruction — "...: Permission
    # denied. Nothing has been deleted. Fix its permissions or its mount, then
    # retry." — so the reader is reassured before being told what to do.
    assert detail.endswith(
        ". Nothing has been deleted. Fix its permissions or its mount, then retry."
    ), detail
    # ...and the disk and the library agree with the sentence. The artist arm
    # is Radiohead, which holds TWO albums in the fixture, so the count is what
    # says the whole fan-out was refused rather than only its first album.
    assert len(list(duplicates_lib.albums())) == total_before
    assert duplicates_lib.get_album(album_id) is not None
    assert os.path.isdir(folder), "the album's files are still where the library says"
    assert not trash.exists(), "no Trash name was allocated and no Trash dir created"
    if shape == "file":
        assert origins.read_bytes() == b"not a directory", "the store is as it was"
    else:
        assert sorted(p.name for p in origins.iterdir()) == ["Someone Elses.json"]


def test_a_fanout_that_already_moved_one_album_does_not_promise_nothing_was_deleted(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store going unusable BETWEEN two albums answers 500 without the promise.

    "Nothing has been deleted" used to live in the store refusal's own sentence
    (``trash_origins._STORE_FIX``), which every mover relays. The fan-out asks
    the store once PER ALBUM, so album 2's refusal was relayed into a body that
    said, forty words apart, that one album had been moved to Trash and that
    nothing had been deleted — in the one field the browser renders. The promise
    now belongs to the two 503 arms, which fire only before anything is created,
    moved or dropped, and this is the run that must not have it.

    The seam is placed by wrapping the guard, but the refusal itself is REAL: the
    wrapper replaces the store with a regular FILE between the two albums and
    then calls the guard, so the sentence the 500 relays is the store's own —
    which is the only way this test can see where that sentence puts the promise.
    The FILE shape rather than a chmod because it denies for uid 0 too, so the
    test does not self-skip for a maintainer running this suite inside the
    shipped image, which declares no ``USER`` (``Dockerfile``).
    """
    trash = tmp_path / "trash"
    calls: list[Path] = []
    real = require_usable_store  # the same object ``trash`` imported, before the patch

    def _breaks_the_store_before_the_second_album(origins_dir: Path) -> None:
        calls.append(origins_dir)
        if len(calls) > 1:
            shutil.rmtree(origins_dir)
            origins_dir.write_bytes(b"not a directory")
        real(origins_dir)

    monkeypatch.setattr(
        trash_mod, "require_usable_store", _breaks_the_store_before_the_second_album
    )
    req = _store_fault_req(duplicates_lib, tmp_path, trash)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_artist_op(req, "Radiohead"))  # type: ignore[arg-type]  # stub req

    assert len(calls) == 2, "one ask per album, and the fan-out stopped on the second"
    assert ei.value.status_code == 500, "the 503 tier closed the moment an album was dropped"
    detail = ei.value.detail
    assert isinstance(detail, dict), "the structured body, not the 503's flat sentence"
    message = detail["message"]
    assert "Nothing has been deleted" not in message, "one album HAS been"
    assert "trash-origins" in message, "the cause is still relayed, in the store's own words"
    assert "is not a usable folder" in message, "...naming which question the store failed"
    assert "1 of 2 albums had been moved to Trash" in message, "...and how far it got"
    # ...and the disk agrees with the half the message does claim.
    assert len(list(trash.iterdir())) == 1, "the first album really is in Trash"
    remaining = [a.album for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]
    assert len(remaining) == 1, "the second album kept its rows"


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
        "Library folder is present but none of the music files the library names"
        " are in it. Either the music share is not mounted, or those files have"
        " been removed outside MusicDrop."
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

# The recovery line every failure that is not one of the four the ``_recovery``
# chain names gets. Spelled once here, asserted at each of the FOUR states that
# reach it: three where nothing moved, and the per-item fallback stopping
# mid-album (see ``delete._recovery``). It was five — the fifth was the
# ``album.remove`` window with the whole folder in Trash, and that state has its
# own sentence now that the primitive moves the folder back
# (``decisions.md`` 28 item 4), pinned in
# ``test_delete_500_when_the_rows_will_not_go_says_the_files_came_BACK``, which
# asserts INEQUALITY against this constant. The sentence states no disk fact in
# either direction, which is what lets one stand in all four — so an equality
# against this constant is a SPELLING check, and each state's test carries its
# own direction-asserting line beside its disk asserts.
_LOOK_IN_TRASH = (
    "Check the Trash folder before retrying: a delete that stops part-way can"
    " leave some or all of the files there. Retry."
)


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


def _artist_op_500(
    lib: Library, tmp_path: Path, trash: Path, artist: str = _GHOST_ARTIST
) -> HTTPException:
    """Run ``delete_artist_op`` for an artist and return the 500 it raises."""
    handle = make_test_handle(lib, tmp_path)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_artist_op(_Req(), artist))  # type: ignore[arg-type]  # stub req
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
    assert "Either the music share is not mounted, or those files have been" in message
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
    assert detail["recovery"] == _LOOK_IN_TRASH
    assert len([a for a in lib.albums() if a.albumartist == _GHOST_ARTIST]) == 1


def _two_album_artist_library(tmp_path: Path, *, first_on_disk: bool) -> Library:
    """``Twosome``'s two albums, the second of them always really on disk.

    Drives BOTH shapes of the partial message from one construction, since the
    clause under test is the same in both: with ``first_on_disk`` the fan-out
    really moves album one (``moved`` = 1, the "had been moved to Trash" shape);
    without it album one is a ghost row whose folder was removed outside
    MusicDrop (``moved`` = 0, the "no files left to move" shape).

    ``A First`` sorts before ``B Second`` under the same albumartist, so the
    fan-out's order is the fixture's order and the album it stops on is known. A
    bystander album on disk keeps ``require_library_present`` a certainty rather
    than a draw: three albums against a sample of five is an exhaustive draw.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, name: str, on_disk: bool) -> None:
        base = music / artist / album
        base.mkdir(parents=True, exist_ok=True)
        f = base / name
        f.write_bytes(b"\x00")
        item = Item(album=album, albumartist=artist, artist=artist, title="Track", track=1)
        item.path = os.fsencode(str(f))
        lib.add_album([item]).store()
        if not on_disk:
            shutil.rmtree(base)  # removed outside MusicDrop; the row survives

    add(artist="Twosome", album="A First", name="01 First.mp3", on_disk=first_on_disk)
    add(artist="Twosome", album="B Second", name="01 Second.mp3", on_disk=True)
    add(artist="Bystander", album="Still Here", name="01 t.mp3", on_disk=True)
    return lib


@pytest.mark.parametrize(
    ("first_on_disk", "expected"),
    [
        (True, "stopped after 1 of 2 albums had been moved to Trash"),
        (False, "stopped after dropping 1 of 2 albums that had no files left to move"),
    ],
)
def test_delete_artist_partial_speaks_only_of_the_albums_it_never_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_on_disk: bool, expected: str
) -> None:
    """Both partial messages ended "the rest are untouched". The rest includes this one.

    "The rest" takes in the album the fan-out stopped ON, and that album can be
    the most touched of all. Neither counter can see it — both count returns
    from the primitive — so the message says what it does know: the albums it
    never reached.

    The clause stays qualified now that the whole-folder path undoes its own
    ``album.remove`` window (``decisions.md`` 28 item 4), because the undo
    narrows the set rather than emptying it. What this test now pins is the
    boundary the undo draws across a fan-out: the album it stopped on is BACK
    where it came from with nothing of it in Trash, while the album before it
    stays deleted with its files in Trash — beets commits on the way out of the
    transaction even while unwinding, so nothing already done can be taken back,
    and a message claiming otherwise would be the same falsehood in the other
    direction.

    The fault is a patched ``Album.remove`` on the SECOND album, not a plugin
    listener: a listener fires after beets deleted the album row
    (``beets/library/models.py:391`` before ``:394``), so it could not support
    the "still in the library" assert below. Both message shapes, because the
    clause was the same sentence in both and a fix to one of them is not a fix.
    """
    lib = _two_album_artist_library(tmp_path, first_on_disk=first_on_disk)
    trash = tmp_path / "trash"
    second = next(a for a in lib.albums() if a.album == "B Second")
    second_id = _require_id(second.id)
    second_root = Path(album_folder(lib, list(second.items())))
    first_id = _require_id(next(a for a in lib.albums() if a.album == "A First").id)
    calls = {"n": 0}
    real_remove = type(second).remove

    def _boom_on_the_second(self: Album, delete: bool = False, with_items: bool = True) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk I/O error, forced by the fixture")
        real_remove(self, delete, with_items)

    monkeypatch.setattr(type(second), "remove", _boom_on_the_second)

    detail = _artist_op_500(lib, tmp_path, trash, "Twosome").detail

    assert isinstance(detail, dict)
    message = detail["message"]
    assert expected in message  # how far it got, unchanged
    assert "the rest are untouched" not in message
    assert "the albums it never reached are untouched" in message
    # The album it stopped on: back where it came from, nothing of it in Trash.
    assert (second_root / "01 Second.mp3").is_file()
    assert not (trash / "B Second").exists()
    assert lib.get_album(second_id) is not None
    # ...and the album BEFORE it is still gone, which no undo can change.
    assert lib.get_album(first_id) is None
    if first_on_disk:
        assert (trash / "A First" / "01 First.mp3").is_file()
        assert "Trash" in detail["recovery"], "that one really is recoverable from there"


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
    assert detail["recovery"] == _LOOK_IN_TRASH
    # The disk agrees with the sentence: there is no Trash folder to look in.
    assert not trash.exists()
    assert len([a for a in duplicates_lib.albums() if a.albumartist == "Radiohead"]) == 2


@pytest.mark.parametrize("artist", [None, "Daft Punk"])
def test_delete_500_when_the_rows_will_not_go_says_the_files_came_BACK(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artist: str | None
) -> None:
    """``album.remove`` is itself a step, and this is the state it used to leave.

    It used to leave the folder in Trash with its origin record written and the
    album row gone, and the recovery line answered "Nothing reached the Trash
    folder, so there is nothing to restore" — the one user whose album really
    WAS recoverable told to stop looking, on the page whose next button empties
    Trash for good. That sentence was fixed first; the owner then ruled the
    state itself out (``decisions.md`` 28 item 4), so this test asserts the
    opposite of what it used to: the folder is back where it came from and
    there is nothing in Trash at all.

    The mechanism is a patched ``Album.remove``, NOT a plugin listener, and the
    swap is the point rather than convenience. A listener raising on
    ``album_removed`` fires after beets has already deleted the album row
    (``beets/library/models.py:391`` before ``:394``) and the transaction commits
    on the way out, so the final assert below — the album still queryable —
    would be false there for a reason that has nothing to do with the undo. The
    patched remove is the DB-fault shape (a locked or read-only ``library.db``
    raises ``DBAccessError`` before any row is written), which is the realistic
    cause and the one where a move-back yields full consistency. The listener
    case is a recorded residual; see ``BACKLOG.md``.

    Both entry points, because they reach the sentence by different routes: the
    single-album delete lets the exception through, and the fan-out meets it on
    its first album and re-raises it bare (``mutated == 0`` — that counter counts
    returns from the primitive, and this album never returned).
    """
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, tmp_path)
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    album_root = Path(album_folder(duplicates_lib, list(album.items())))
    monkeypatch.setattr(
        type(album),
        "remove",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("disk I/O error, forced by the fixture")
        ),
    )

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    op = (
        delete_artist_op(_Req(), artist)  # type: ignore[arg-type]  # stub req
        if artist is not None
        else delete_album_op(_Req(), album_id)  # type: ignore[arg-type]  # ditto
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    # The cause is relayed. The string is deliberately NOT a paraphrase of the
    # message's own prose: with the fixture raising "library rows could not be
    # removed", dropping the cause from the message left this very assert green
    # (measured), because those words are in the sentence around it.
    assert "disk I/O error, forced by the fixture" in detail["message"]
    # The disk and the body agree, and the body no longer sends this reader to
    # Trash for a folder that is not in it.
    assert (album_root / "01 Track 1.mp3").is_file()
    assert list(trash.iterdir()) == []
    assert list(origins_for(trash).iterdir()) == []
    assert duplicates_lib.get_album(album_id) is not None
    recovery = detail["recovery"]
    assert recovery != _LOOK_IN_TRASH, "this state is no longer one the fallback has to cover"
    assert "moved back" in recovery
    assert "nothing in Trash for this album" in recovery


def test_delete_500_when_the_undo_ALSO_fails_does_not_send_the_reader_to_empty_trash(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The double failure's own recovery line, which nothing else pins.

    The primitive's message is asserted where it is composed
    (``test_trash.py``); what is asserted here is the sentence wrapped round it,
    because the two are chosen independently and the wrong one is dangerous in
    a specific way: the files really ARE in Trash in this state, and the page
    this body renders has an Empty button on it. The fallback hint — "a delete
    that stops part-way can leave some or all of the files there" — reads as
    permission to go and tidy up.
    """
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, tmp_path)
    album = next(a for a in duplicates_lib.albums() if a.albumartist == "Daft Punk")
    album_id = _require_id(album.id)
    album_root = Path(album_folder(duplicates_lib, list(album.items())))

    def _retake_the_origin_then_fail(_self: Album, *_a: object, **_k: object) -> None:
        album_root.mkdir(parents=True)
        (album_root / "a stranger.mp3").write_bytes(b"\x00")
        raise RuntimeError("disk I/O error, forced by the fixture")

    monkeypatch.setattr(type(album), "remove", _retake_the_origin_then_fail)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_album_op(_Req(), album_id))  # type: ignore[arg-type]  # stub req

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "There is something at BOTH places now" in detail["message"]
    recovery = detail["recovery"]
    assert recovery != _LOOK_IN_TRASH, "the fallback reads as permission to tidy Trash up"
    assert "Do NOT empty the Trash folder" in recovery
    # ...and the state that makes that sentence matter is really on the disk.
    assert (trash / "Discovery" / "01 Track 1.mp3").is_file()


def test_a_fanout_that_stops_on_a_double_failure_does_not_send_the_reader_to_empty_trash(
    duplicates_lib: Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper must not swallow the album it stopped ON having its own line.

    ``ArtistDeletePartialError`` used to be answered on ``moved`` alone, so a
    fan-out that got one album into Trash and then stopped on one whose rows
    would not go AND whose folder would not come back shipped "Files are
    recoverable in the Trash folder. Retry." — the one instruction that state
    must not give, on a page with an Empty button, for an album that can be in
    Trash, at its own folder, or half at each. The promise about album 1 is
    still true; it is simply not the sentence this reader needs first.

    Both albums belong to Radiohead in the fixture, so the fan-out really does
    reach a second one. The origin is retaken during the second album's
    ``remove`` — the window ``move_no_merge`` exists for — which is what turns
    the undo into the double failure.
    """
    trash = tmp_path / "trash"
    roots = {
        _require_id(a.id): Path(album_folder(duplicates_lib, list(a.items())))
        for a in duplicates_lib.albums()
        if a.albumartist == "Radiohead"
    }
    assert len(roots) == 2, "the fan-out needs a second album to stop on"
    real_remove = Album.remove
    seen: list[int] = []

    def _retakes_the_origin_on_the_second_album(
        self: Album, *args: object, **kwargs: object
    ) -> None:
        seen.append(_require_id(self.id))
        if len(seen) == 1:
            real_remove(self, *args, **kwargs)  # type: ignore[arg-type]  # passthrough
            return
        root = roots[_require_id(self.id)]
        root.mkdir(parents=True)
        (root / "a stranger.mp3").write_bytes(b"\x00")
        raise RuntimeError("disk I/O error, forced by the fixture")

    monkeypatch.setattr(Album, "remove", _retakes_the_origin_on_the_second_album)
    req = _store_fault_req(duplicates_lib, tmp_path, trash)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_artist_op(req, "Radiohead"))  # type: ignore[arg-type]  # stub req

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "1 of 2 albums had been moved to Trash" in detail["message"], "it really is partial"
    assert "There is something at BOTH places now" in detail["message"], "...on a double failure"
    recovery = detail["recovery"]
    assert recovery != "Files are recoverable in the Trash folder. Retry.", "not the promise"
    assert recovery != _LOOK_IN_TRASH, "nor the fallback"
    assert "Do NOT empty the Trash folder before reading the message above" in recovery
    # ...and the state that makes the sentence matter is on the disk.
    stopped_on = roots[seen[1]]
    assert (stopped_on / "a stranger.mp3").is_file(), "something is at the album's own folder"
    assert len(list(trash.iterdir())) == 2, "and both albums are in Trash, one of them stranded"


def _shared_folder_two_track_library(tmp_path: Path) -> Library:
    """An album of TWO tracks in a folder it shares, so a move can stop HALF done.

    ``trash_album_folder``'s whole-folder branch is one ``shutil.move``: it
    happened or it did not. The shared-folder fallback moves item by item and
    beets stores each item's new path as it goes (``Album.move`` ->
    ``item.move(..., store=True)``, ``beets/library/models.py:489-495``), so a
    fault on the second item leaves the first under Trash with its row already
    pointing there and the album still in the library.

    * ``A Two`` and ``B Live`` are both ``Sharey``'s and share
      ``music/Sharey/Both``, which is what makes ``_folder_is_shared`` refuse
      the whole-folder move;
    * ``A Two`` sorts first under the same albumartist, so the artist fan-out
      meets it as its FIRST album — the tier where ``mutated == 0``;
    * a bystander album on disk keeps ``require_library_present`` a certainty
      rather than a draw: three albums against a sample of five is exhaustive.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, at: Path, names: list[str]) -> None:
        at.mkdir(parents=True, exist_ok=True)
        items = []
        for n, name in enumerate(names, 1):
            f = at / name
            f.write_bytes(b"\x00")
            item = Item(album=album, albumartist=artist, artist=artist, title=f"T{n}", track=n)
            item.path = os.fsencode(str(f))
            items.append(item)
        lib.add_album(items).store()

    both = music / "Sharey" / "Both"
    add(artist="Sharey", album="A Two", at=both, names=["01 A.mp3", "02 A.mp3"])
    add(artist="Sharey", album="B Live", at=both, names=["03 B.mp3"])
    add(
        artist="Bystander",
        album="Still Here",
        at=music / "Bystander" / "Album",
        names=["01 t.mp3"],
    )
    return lib


def test_delete_album_500_does_not_read_the_answer_out_of_a_half_moved_album(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something IS in Trash here, and none of the old sentence's reading holds.

    The recovery line used to answer the user's question for them: "if the
    album's folder is there it can be restored from there; if it is not, nothing
    moved and there is nothing to restore". This is the state where the first
    half is as wrong as the second. What reached Trash is the CONTAINER named
    for the album, holding the one item that made it — not the album's folder;
    no origin record was written, because ``trash_album`` writes one only after
    the move; and the album is still in the library with that item's row
    pointing inside Trash.

    ``Item.move`` is patched rather than the primitive, so the half-moved state
    is built by the real mover: item one really is relocated and stored before
    item two raises.
    """
    from beets.library import Item
    from beets.util import MoveOperation

    lib = _shared_folder_two_track_library(tmp_path)
    trash = tmp_path / "trash"
    handle = make_test_handle(lib, tmp_path)
    album_id = _require_id(next(a for a in lib.albums() if a.album == "A Two").id)
    real_move = Item.move
    calls = {"n": 0}

    def _fails_on_the_second(
        item: Item,
        operation: MoveOperation = MoveOperation.MOVE,
        basedir: bytes | None = None,
        with_album: bool = True,
        store: bool = True,
    ) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError(13, "Permission denied")
        real_move(item, operation, basedir=basedir, with_album=with_album, store=store)

    monkeypatch.setattr(Item, "move", _fails_on_the_second)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_album_op(_Req(), album_id))  # type: ignore[arg-type]  # stub req

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "Permission denied" in detail["message"]  # the cause is relayed
    assert detail["recovery"] == _LOOK_IN_TRASH
    # The direction, beside the state below: the sentence may point AT Trash and
    # may not tell this user what finding something there means.
    assert "can be restored" not in detail["recovery"]
    assert "nothing to restore" not in detail["recovery"]
    # The state itself. One file made it, under the container rather than under
    # anything named like the album's folder; the row for it points into Trash;
    # the album never left the library; and nothing recorded where it came from.
    container = trash / "Sharey - A Two"
    assert len(list(container.rglob("*.mp3"))) == 1
    album = lib.get_album(album_id)
    assert album is not None  # the rows were kept
    rows = [os.fsdecode(i.path) for i in album.items()]
    assert [r for r in rows if r.startswith(f"{container}{os.sep}")] != []
    assert not list(origins_for(trash).glob("*.json"))
    # ...and the album that only ever shared the folder is untouched.
    assert (tmp_path / "music" / "Sharey" / "Both" / "03 B.mp3").is_file()


def _shared_folder_ghost_library(tmp_path: Path) -> Library:
    """Two albums of one artist in ONE folder, the first of them files-less.

    The shape that reaches ``trash_album``'s ghost arm — the only branch that
    hands ``_reached_trash`` a path in the MUSIC dir rather than at or under
    Trash:

    * ``A Ghost`` and ``B Live`` are both ``Sharey``'s and both live in
      ``music/Sharey/Both``, so ``_folder_is_shared`` refuses the whole-folder
      move and falls back to the per-item ``trash_album``;
    * ``A Ghost``'s file is removed while ``B Live``'s stays, so the folder is
      still a directory (the missing-folder branch is skipped) and yet nothing
      of ``A Ghost``'s can move;
    * a bystander album on disk keeps ``require_library_present`` a certainty —
      three albums against a sample of five is an exhaustive draw, and two of
      them are really there.

    ``A Ghost`` sorts before ``B Live`` under the same albumartist, so the
    fan-out meets it first.
    """
    from beets.library import Item

    music = tmp_path / "music"
    lib = build_library(str(tmp_path / "library.db"), str(music))

    def add(*, artist: str, album: str, at: Path, name: str) -> Path:
        at.mkdir(parents=True, exist_ok=True)
        f = at / name
        f.write_bytes(b"\x00")
        item = Item(album=album, albumartist=artist, artist=artist, title="Track", track=1)
        item.path = os.fsencode(str(f))
        lib.add_album([item]).store()
        return f

    both = music / "Sharey" / "Both"
    gone = add(artist="Sharey", album="A Ghost", at=both, name="01 Ghost.mp3")
    add(artist="Sharey", album="B Live", at=both, name="02 Live.mp3")
    add(artist="Bystander", album="Still Here", at=music / "Bystander" / "Album", name="01 t.mp3")
    gone.unlink()  # removed outside MusicDrop; the row and the shared folder survive
    return lib


def test_delete_artist_does_not_count_a_shared_folder_ghost_as_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path OUTSIDE Trash is not a Trash entry, however unlike ``trash_dir`` it looks.

    ``_reached_trash`` asks two things of the primitive's answer: that it is not
    ``trash_dir`` itself, and that it is under it. The first alone is what the
    two row-dropping branches of ``trash_album_folder`` need — they return
    ``str(trash_dir)`` — and it is all the ghost test above exercises. This is
    the case only the second catches: ``trash_album``'s own ghost arm returns
    the album's folder in the MUSIC dir, which is neither ``trash_dir`` nor
    inside it.

    Counted as a move, the fan-out's 500 would say one album "had been moved to
    Trash" and send its user to a Trash folder holding nothing at all — the
    exact promise the two counters exist to keep apart.
    """
    lib = _shared_folder_ghost_library(tmp_path)
    trash = tmp_path / "trash"
    handle = make_test_handle(lib, tmp_path)
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
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    with pytest.raises(HTTPException) as ei:
        asyncio.run(delete_artist_op(_Req(), "Sharey"))  # type: ignore[arg-type]  # stub req

    assert ei.value.status_code == 500
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert "1 of 2" in detail["message"]  # one album's rows went, and it says so
    assert "moved to Trash" not in detail["message"]  # but nothing was moved
    assert detail["recovery"] == _LOOK_IN_TRASH
    # The premise of the whole test, asserted rather than assumed: the ghost went
    # through the SHARED-folder fallback, whose sibling is still sitting in the
    # folder it was never allowed to move wholesale — and Trash is empty.
    assert sorted(p.name for p in (tmp_path / "music" / "Sharey" / "Both").iterdir()) == [
        "02 Live.mp3"
    ]
    assert list(trash.iterdir()) == []
    assert [a.album for a in lib.albums() if a.albumartist == "Sharey"] == ["B Live"]


@pytest.mark.parametrize(
    ("path", "artist"),
    [("/api/albums/{album_id}", None), ("/api/artists", "Radiohead")],
)
def test_the_delete_routes_500_description_does_not_deny_its_own_body(
    duplicates_lib: Library,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    artist: str | None,
) -> None:
    """The schema's 500 may not promise what a real 500 body denies.

    Both routes DECLARED "its files are recoverable in the Trash folder" flatly,
    for a status whose structured body carries the recovery line that decides
    exactly that — so the generated client's types documented one answer while
    the wire sent the other. The tests above pin the body; this joins it to the
    contract, so the two cannot drift apart again.

    Two halves, and neither is evidence on its own: the LIVE spec's description
    (a wrong one passes every behavioural test in this file) against a REAL 500
    provoked with nothing moved (a wrong body passes any assertion about the
    spec). ``tests/test_openapi_spec_guard.py`` is not evidence about either —
    it only fires on a dump that was not regenerated.
    """
    from app.main import app

    def _refuses(*args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(delete_mod, "trash_album_folder", _refuses)
    trash = tmp_path / "trash"
    handle = make_test_handle(duplicates_lib, tmp_path)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    op = (
        delete_artist_op(_Req(), artist)  # type: ignore[arg-type]  # stub req
        if artist is not None
        else delete_album_op(_Req(), _require_id(next(iter(duplicates_lib.albums())).id))  # type: ignore[arg-type]  # ditto
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail["recovery"] == _LOOK_IN_TRASH
    assert not trash.exists()  # nothing moved here, and the body promises nothing

    description = app.openapi()["paths"][path]["delete"]["responses"]["500"]["description"]
    assert "recoverable in the Trash folder" not in description, (
        f"{path}'s 500 description promises Trash recovery for a status this route"
        f" answers with {detail['recovery']!r}"
    )
    # The negative alone is happy with a paraphrase that drops the condition —
    # "the body carries the cause and a recovery hint" passes it and tells the
    # reader of the contract nothing about WHEN Trash gets named. The condition
    # is the claim, so assert it directly.
    #
    # The verb is part of the claim. "points at the Trash folder only when" was
    # false against this very body: ``_LOOK_IN_TRASH`` names the Trash folder for
    # the moved-nothing case asserted above. What is conditional is the PROMISE,
    # not the mention — ``_recovery``'s docstring draws the line as promise vs
    # check — so both descriptions say "promises recovery from", and this asserts
    # the whole phrase rather than the bare condition.
    assert "promises recovery from the Trash folder only when" in description, (
        f"{path}'s 500 description must keep the condition on its Trash promise;"
        f" it reads {description!r}"
    )


@pytest.mark.parametrize(
    ("path", "artist", "promise"),
    [
        ("/api/albums/{album_id}", None, "The album is still in the library"),
        ("/api/artists", "Sharey", "None of the artist's albums has been dropped"),
    ],
)
def test_the_delete_routes_503_description_does_not_deny_a_share_that_dropped_mid_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    artist: str | None,
    promise: str,
) -> None:
    """Both 503s DECLARED "Nothing reached the Trash folder". This is a 503 that did.

    The guard is not only a pre-check: ``trash.py``'s post-condition re-checks
    the root BEFORE it asks whether anything landed, so a share that drops
    part-way through the per-item move raises ``LibraryRootUnavailableError``
    with items already under the Trash container — and both ops answer that with
    the 503, ahead of the blanket 500. The artist route's own extra clause ("a
    share that drops part-way through the fan-out is reported as the 500
    instead") is wrong for the same run: the fan-out has dropped nothing yet, so
    ``mutated == 0`` re-raises the cause bare and it lands here too.

    What every 503 does share is the library: no rows are gone. That is what the
    descriptions now claim, and this test holds both halves together — the LIVE
    spec against a REAL 503 with files under Trash and the album still in the
    library. Neither half is evidence on its own, and
    ``tests/test_openapi_spec_guard.py`` is evidence about neither: it only
    fires on a dump that was not regenerated.

    The share is dropped by the real mover, between the first item and the
    second, so nothing here decides where the raise comes from.
    """
    from beets.library import Item
    from beets.util import MoveOperation

    from app.main import app

    lib = _shared_folder_two_track_library(tmp_path)
    music = tmp_path / "music"
    trash = tmp_path / "trash"
    handle = make_test_handle(lib, tmp_path)
    album_id = _require_id(next(a for a in lib.albums() if a.album == "A Two").id)
    real_move = Item.move
    calls = {"n": 0}

    def _drops_the_share_after_the_first(
        item: Item,
        operation: MoveOperation = MoveOperation.MOVE,
        basedir: bytes | None = None,
        with_album: bool = True,
        store: bool = True,
    ) -> None:
        real_move(item, operation, basedir=basedir, with_album=with_album, store=store)
        calls["n"] += 1
        if calls["n"] == 1:
            shutil.rmtree(music)  # the share goes between item one and item two

    monkeypatch.setattr(Item, "move", _drops_the_share_after_the_first)

    class _App:
        state = SimpleNamespace(
            beets_library=handle,
            settings=Settings(trash_dir=str(trash), trash_origins_dir=str(origins_for(trash))),
        )

    class _Req:
        app = _App()

    op = (
        delete_artist_op(_Req(), artist)  # type: ignore[arg-type]  # stub req
        if artist is not None
        else delete_album_op(_Req(), album_id)  # type: ignore[arg-type]  # ditto
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(op)

    assert ei.value.status_code == 503
    # The root guard's own flat sentence, so this is the arm the descriptions
    # below describe and not some other 503.
    assert ei.value.detail == "Library folder unavailable. Is the music share mounted?"
    # The state the old sentence denied: one item is under the Trash container.
    assert len(list((trash / "Sharey - A Two").rglob("*.mp3"))) == 1
    # ...and the state both descriptions may still promise: the rows are kept.
    assert lib.get_album(album_id) is not None

    description = app.openapi()["paths"][path]["delete"]["responses"]["503"]["description"]
    assert "Nothing reached the Trash folder" not in description, (
        f"{path}'s 503 description denies a Trash entry this same status leaves behind;"
        f" it reads {description!r}"
    )
    # The negative alone is happy with a description that says nothing about
    # either side. Both halves of the replacement are the claim, so assert them:
    # what is known (no rows are gone) and what is not (go and look).
    assert promise in description
    assert "check there before retrying" in description
