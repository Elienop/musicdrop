"""Delete (reversible Trash) for whole albums and whole artists.

Front-door delete: move an album's — or every album of an artist's — ENTIRE
folder to Trash (audio + art + ``.lrc``/``.txt`` sidecars + extras) and drop it
from the library, built on :func:`app.beets.trash.trash_album_folder`.

The async ``_op`` functions mirror the duplicates-resolve op: gated behind the
SAME library-job lock + 409 (Apply / import / lyrics / artist-art / reorganize),
run the blocking move in a threadpool, and bind ``music_dir_context`` so beets'
relative item paths resolve on the worker thread (which doesn't inherit the
``beets.context`` ContextVar — same gap as resolve/Apply).
"""

from __future__ import annotations

from pathlib import Path

from beets.library import Library
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.beets.config_editor import _settings, _swap_lock
from app.beets.library import (
    LibraryHandle,
    LibraryRootUnavailableError,
    _coerce_str,
    _require_id,
    require_library_root,
)
from app.beets.trash import resolve_trash_dir, resolve_trash_origins_dir, trash_album_folder
from app.library_busy import library_job_active
from app.models.delete import DeleteResult


class AlbumNotFoundError(Exception):
    """A referenced album id is not in the library. Maps to 404."""


class ArtistDeletePartialError(Exception):
    """The artist fan-out mutated some albums and then could not finish.

    Deliberately NOT a :class:`~app.beets.library.LibraryRootUnavailableError`,
    even though that is what caused it: the 503 those map to promises that
    nothing was moved or dropped, which stops being true the moment one album
    has been through the primitive — and beets commits on the way out of the
    transaction even while unwinding the exception, so the work already done
    cannot be taken back. Falls to the blanket 500 instead, whose message says
    how far the fan-out got.
    """


def delete_album(
    lib: Library,
    album_id: int,
    *,
    trash_dir: Path,
    origins_dir: Path,
    dropped_item_ids: set[int] | None = None,
) -> DeleteResult:
    """Move one album's whole folder to Trash and drop it. 404 on unknown id.

    Binds ``music_dir_context`` (the threadpool thread doesn't inherit it, so the
    move would otherwise get a relative source path); one transaction so the DB
    drop commits with the file move.

    ``dropped_item_ids``, when given, collects the ids of the items this delete
    removes — read BEFORE the trash call, because that call drops the rows and
    ``album.items()`` would then be empty. The caller owes those items' playlists
    a `.m3u8` re-export (the export lists a file that is now in Trash), and the
    out-set is how it learns which ids to pass. Same accumulate-into-a-caller's-
    collection shape the reorganize sweep uses for ``vacated``.
    """
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        if dropped_item_ids is not None:
            dropped_item_ids.update(_require_id(i.id) for i in album.items())
        with lib.transaction():
            trash_path = trash_album_folder(
                lib, album, trash_dir=trash_dir, origins_dir=origins_dir
            )
    return DeleteResult(trashed_albums=1, trash_path=trash_path)


def delete_artist(
    lib: Library,
    artist_name: str,
    *,
    trash_dir: Path,
    origins_dir: Path,
    dropped_item_ids: set[int] | None = None,
) -> DeleteResult:
    """Move EVERY album of ``artist_name`` (matched on albumartist) to Trash.

    Album ids are snapshotted before mutating (trashing drops rows). An artist
    with no albums is a safe no-op (``trashed_albums=0``). One transaction wraps
    all the moves so they COMMIT TOGETHER — one commit point, never a rollback:
    beets' ``Transaction.__exit__`` commits unconditionally, including when it is
    unwinding an exception (``beets/dbcore/db.py:924-941``). So a fault part-way
    through leaves the albums already processed trashed and dropped, and no
    amount of error handling here can undo them.

    That shapes how the unmounted share is reported, in two tiers:

    * **before the first mutation** — the check below runs ahead of the
      transaction, and the primitive re-checks per album, so a root that is
      already unavailable (or drops before the first move) raises
      ``LibraryRootUnavailableError``, which the op answers with a 503 that
      truthfully says nothing was moved or dropped;
    * **after at least one album** — the same cause is re-raised as
      :class:`ArtistDeletePartialError`, because the 503's promise is no longer
      true. It reaches the user as the 500, naming how far the fan-out got.

    ``dropped_item_ids`` collects the ids this fan-out removes (see
    :func:`delete_album`), filled PER ALBUM inside the loop rather than up front:
    a partial run must report exactly the playlists it really invalidated, and an
    album that raised before its own capture never contributed one.
    """
    with lib.music_dir_context():
        target = artist_name.strip()
        album_ids = [
            _require_id(a.id) for a in lib.albums() if _coerce_str(a.albumartist).strip() == target
        ]
        # Ahead of the transaction, not only inside the primitive: this is what
        # makes the 503's "nothing was dropped" a property of the whole
        # operation rather than a property of whichever album happened to be
        # first. Cheap (one isdir + one scandir entry) against N folder moves.
        require_library_root(lib)
        trashed = 0
        with lib.transaction():
            for album_id in album_ids:
                album = lib.get_album(album_id)
                if album is None:
                    continue
                if dropped_item_ids is not None:
                    dropped_item_ids.update(_require_id(i.id) for i in album.items())
                try:
                    trash_album_folder(lib, album, trash_dir=trash_dir, origins_dir=origins_dir)
                except LibraryRootUnavailableError as exc:
                    if trashed == 0:
                        raise  # nothing mutated yet — the honest 503 still holds
                    raise ArtistDeletePartialError(
                        f"the music share became unavailable after {trashed} of "
                        f"{len(album_ids)} albums had been moved to Trash; the rest are "
                        f"untouched ({exc})"
                    ) from exc
                except Exception as exc:
                    # The same two tiers for ANY other cause. Only the unmounted
                    # share used to get them, so a permission error, a full disk
                    # or a DB fault mid-fan-out reached the user as a bare
                    # message with no idea how far the delete had got — while
                    # beets had already committed every album before it.
                    #
                    # "The rest are untouched" is true of the album this stopped
                    # ON as well, which is not obvious and is worth stating: the
                    # only step between the folder move and the row drop is the
                    # origin record, and ``_record_origin`` swallows everything
                    # by design, so there is no window that leaves an album's
                    # files in Trash while its rows survive.
                    if trashed == 0:
                        raise  # nothing mutated yet — the caller's own error still holds
                    raise ArtistDeletePartialError(
                        f"the delete stopped after {trashed} of {len(album_ids)} albums had"
                        f" been moved to Trash; the rest are untouched ({exc})"
                    ) from exc
                trashed += 1
    return DeleteResult(trashed_albums=len(album_ids), trash_path=str(trash_dir))


def _gate() -> None:
    """Refuse (409) while any library job that mutates the library is running.

    Same set as config Apply / duplicates resolve — a delete tears at the same
    files + DB those jobs touch, so it must not overlap them.
    """
    if library_job_active():
        raise HTTPException(
            status_code=409,
            detail="A library operation is in progress; delete available when it finishes",
        )


def _failed(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={
            "message": f"Delete failed: {exc}",
            "recovery": "Files are recoverable in the Trash folder. Retry.",
        },
    )


async def delete_album_op(
    request: Request, album_id: int, dropped_item_ids: set[int] | None = None
) -> DeleteResult:
    """Async wrapper for :func:`delete_album`: gate + swap-lock + threadpool.

    ``dropped_item_ids`` is passed straight through so the endpoint can re-export
    the `.m3u8` of every playlist holding a track this delete removed. It is only
    ever read on the success path: every failure exit here raises, so a 503/500
    caller never re-exports off a delete that did not (fully) happen.
    """
    app = request.app
    _gate()
    async with _swap_lock(app):
        handle: LibraryHandle = app.state.beets_library
        trash_dir = resolve_trash_dir(_settings(app), handle)
        try:
            return await run_in_threadpool(
                delete_album,
                handle.lib,
                album_id,
                trash_dir=trash_dir,
                origins_dir=resolve_trash_origins_dir(_settings(app), handle),
                dropped_item_ids=dropped_item_ids,
            )
        except AlbumNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Ahead of the blanket except on purpose: ``_failed``'s recovery line
        # promises the files are in Trash, which is exactly false here — the
        # guard fires before anything moves or is dropped, so nothing is in
        # Trash and nothing needs recovering. 503 with the guard's own flat
        # sentence instead, matching what the disk-sync preview already answers
        # for this same cause (app/api/disk_sync.py). Raised inline rather than
        # through a helper like ``_failed`` so the status stays a literal that
        # tests/test_route_status_declarations.py can see.
        except LibraryRootUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise _failed(exc) from exc


async def delete_artist_op(
    request: Request, artist_name: str, dropped_item_ids: set[int] | None = None
) -> DeleteResult:
    """Async wrapper for :func:`delete_artist`: gate + swap-lock + threadpool.

    ``dropped_item_ids`` behaves as in :func:`delete_album_op`. Note the fan-out's
    PARTIAL failure (``ArtistDeletePartialError`` -> 500) still leaves the albums
    it already trashed out of any re-export: the endpoint never gets to run the
    collateral. Nothing here can fix that — the response is an error, so there is
    no result to carry a count on.
    """
    app = request.app
    _gate()
    async with _swap_lock(app):
        handle: LibraryHandle = app.state.beets_library
        trash_dir = resolve_trash_dir(_settings(app), handle)
        try:
            return await run_in_threadpool(
                delete_artist,
                handle.lib,
                artist_name,
                trash_dir=trash_dir,
                origins_dir=resolve_trash_origins_dir(_settings(app), handle),
                dropped_item_ids=dropped_item_ids,
            )
        # Same 503-before-the-blanket-500 ordering as delete_album_op above,
        # and it matters more here: this one fans across every album.
        except LibraryRootUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise _failed(exc) from exc
