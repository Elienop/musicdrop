"""Trash management API: list / restore / empty (Settings → Trash).

Mirrors the delete op's mutual exclusion. Restore moves a folder out of Trash
(back to its recorded origin, or through a move-import) and empty
rm -rf's trashed folders, so the two must never touch the same tree at once: both
refuse (409) while any library job runs OR the beets swap lock is held, and both
hold that lock across their synchronous file work — so a restore and an empty (in
either order) serialize instead of racing. Every folder argument flows through
``resolve_trash_child`` (404 on traversal) — these are rm -rf / import targets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final, NamedTuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.beets.config_editor import _settings, _swap_lock
from app.beets.import_session import ImportConfigBusyError
from app.beets.library import LibraryHandle, LibraryRootUnavailableError, _music_dir
from app.beets.protected import ProtectedTreeError, ProtectedTrees
from app.beets.store_layout import (
    StoreLayoutError,
    checked_protected_trees,
    checked_reachable_store_dirs,
    checked_store_dirs,
)
from app.beets.trash_manage import (
    TrashEmptyPartialError,
    TrashEntryUnreadableError,
    empty_all,
    empty_one,
    list_trashed_albums,
    resolve_trash_child,
    restore_album,
)
from app.events.emit import emit_library_changed
from app.library_busy import raise_if_library_busy
from app.models.errors import ErrorDetail
from app.models.trash import EmptyResult, RestoreRequest, RestoreResult, TrashListing
from app.wire import AmbiguousDisplayName

router = APIRouter(tags=["trash"])

#: The OpenAPI entries for the two ``_child_or_404`` routes. The 409 covers
#: both distinct refusals those routes can make: the shared gate (a library
#: job running or the beets swap lock held) and the ambiguous-name guard in
#: ``_child_or_404`` itself.
_TRASH_CONFLICT_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "The operation was refused because a library operation is in progress "
        "or the beets swap lock is held, or two trashed folders display under "
        "the same name."
    ),
}
_TRASH_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "The named folder is not in the Trash.",
}
_TRASH_RESTORE_FAILED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "The restore failed because re-importing the trashed folder failed.",
}
#: Every route here resolves the Trash / origin-store pair per request and runs
#: the containment check on what it resolved to, so every one of them can answer
#: 503. The two EMPTY routes get this one, and it says nothing about what was
#: left: ``DELETE /api/trash/all`` removes every unprotected entry BEFORE it
#: refuses. "Kept" rather than "store-layout or identity", because the Empty
#: routes also refuse an entry whose files the LIBRARY still lists, which is
#: neither.
_TRASH_LAYOUT_REFUSED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "The entry was kept; the message names the cause and what to do.",
}
#: The LISTING's own, because "the entry was kept" is false for it: it removes
#: nothing and keeps nothing, it only could not read the Trash it resolved.
_TRASH_LIST_REFUSED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "Trash could not be listed; the message names the setup fault.",
}
#: The single delete's twin of the sweep's failed-entry 500: the entry is still
#: in Trash, and the fault is on disk rather than in the request.
_TRASH_EMPTY_FAILED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "The entry could not be removed; the message names the fault.",
}
#: The sweep's 500, which has TWO causes: entries it could not remove, and a
#: fault that ended it — the root open, or a record it could not drop after an
#: entry went. One sentence for both, because OpenAPI carries one per status.
_TRASH_EMPTY_PARTIAL_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "Trash was not fully cleared; the message names the entries still there or the fault."
    ),
}
#: A move-back restore writes INTO the music library, so it answers an
#: unavailable music share the way delete does rather than falling into the
#: blanket 500. Restore answers both causes; the delete routes answer the
#: identity guard. OpenAPI carries one description per status, so this one says neither
#: and points at the message.
_TRASH_LIBRARY_UNAVAILABLE_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "The folder was not moved out of Trash; the message says which setup fault refused it."
    ),
}


def _required(protected: ProtectedTrees | None) -> ProtectedTrees:
    """The set a ``_store(app, protected=True)`` built."""
    if protected is None:  # pragma: no cover - a caller that forgot the keyword
        raise RuntimeError("this route needs the protected set")
    return protected


def _gate(app: Any) -> None:
    """Refuse (409) while any library-mutating job runs OR the beets swap lock is
    held (mirrors delete._gate via the shared api-layer gate).

    The swap-lock arm is load-bearing here: ``restore_album`` runs its
    synchronous re-import under ``_swap_lock`` but never registers as a library
    job, so a job-only check let Empty-Trash ``rmtree`` the folder a live Restore
    was mid-move on — an irreversible loss ``raise_if_library_busy`` closes.
    """
    raise_if_library_busy(app)


class CheckedTrash(NamedTuple):
    """What one Trash request checked, in the order the routes read it.

    ``protected`` is ``None`` for the two routes that move or remove nothing:
    building the set is a dozen stats, and ``checked_protected_trees``' own
    docstring is what promises the read-only callers do not pay them.
    """

    handle: LibraryHandle
    trash_dir: Path
    origins_dir: Path
    protected: ProtectedTrees | None


def _store(app: Any, *, protected: bool = False) -> CheckedTrash:
    """The handle and the CHECKED Trash / origin-store pair, or a 503.

    Every route in this file resolves that pair, and ``resolve()`` follows
    whatever the path points at NOW — so the boot-time check says nothing about
    this request. Replacing ``<M>/.trash`` with a symlink to ``<M>`` after
    startup was measured to make ``DELETE /api/trash/all`` answer 200 and empty
    the music library, and ``DELETE /api/trash?folder=...`` delete a live artist.

    503 rather than 409 or 500: the same tier the store's own
    ``TrashOriginsStoreUnusableError`` uses, for the same reason — nothing has
    been moved or removed, and the fix is on the operator's side, not a retry.
    Raised inline so the status stays a literal
    ``tests/test_route_status_declarations.py`` can see.
    """
    handle: LibraryHandle = app.state.beets_library
    settings = _settings(app)
    # One arm for both: ``checked_protected_trees`` CREATES the Trash when it is
    # absent, and refuses the same way when it cannot (a link below the music
    # root, or a chain it cannot write). The read arm asks the SAME layout
    # question without creating anything — the rows alone accepted a chain that
    # reaches into the library through a link, so the listing enumerated the
    # attacker's directory while every write here answered 503 (security seat
    # M-1).
    try:
        trees = None
        if protected:
            trash_dir, origins_dir = checked_store_dirs(settings, handle)
            trees = checked_protected_trees(
                settings, handle, trash_dir=trash_dir, origins_dir=origins_dir
            )
        else:
            trash_dir, origins_dir = checked_reachable_store_dirs(settings, handle)
    except StoreLayoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # The same tier, from the guard that refuses to CREATE a Trash inside a
    # library whose music is not there: a directory left on a bare mountpoint
    # defeats the cheap mounted-check for every later caller (security seat
    # H-1). Its own sentence, which is the one the README documents.
    except LibraryRootUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return CheckedTrash(handle, trash_dir, origins_dir, trees)


def _child_or_404(app: Any, folder: str) -> tuple[CheckedTrash, Path]:
    """What the request checked, and the resolved child inside it.

    The pair is returned rather than re-taken by the caller because the child is
    what gets ``rmtree``'d or moved: deriving it from one check and acting under
    a second is two instants where the route can only honestly claim one. It also
    halves the work — a bare ``checked_store_dirs`` walks the whole rule (one row
    per refused relationship, plus two per app store), and both routes were
    paying it twice.
    """
    checked = _store(app, protected=True)
    try:
        return checked, resolve_trash_child(checked.trash_dir, folder)
    except AmbiguousDisplayName:
        # Two trashed folders whose names are not valid UTF-8 can display
        # identically. Restoring or deleting the wrong one is irreversible, so
        # refuse and say how to break the tie.
        raise HTTPException(
            status_code=409,
            detail=(
                "Two trashed folders display under the same name because their names are "
                "not valid UTF-8. Rename one on disk to tell them apart."
            ),
        ) from None
    except ValueError:
        raise HTTPException(status_code=404, detail="Not in Trash") from None


@router.get("/trash", responses={503: _TRASH_LIST_REFUSED_RESPONSE})
async def list_trash(request: Request) -> TrashListing:
    """List the albums sitting in Trash (read off disk; no gate)."""
    app = request.app
    checked = _store(app)
    albums = await run_in_threadpool(
        list_trashed_albums,
        checked.trash_dir,
        origins_dir=checked.origins_dir,
        music_dir=_music_dir(checked.handle.lib),
    )
    # The origins dir is deliberately NOT on the wire beside ``trash_path``: it
    # is an implementation detail of where the records live, and adding a field
    # here would be a contract change for something no UI shows.
    return TrashListing(albums=albums, trash_path=str(checked.trash_dir))


@router.post(
    "/trash/restore",
    responses={
        409: _TRASH_CONFLICT_RESPONSE,
        404: _TRASH_NOT_FOUND_RESPONSE,
        500: _TRASH_RESTORE_FAILED_RESPONSE,
        503: _TRASH_LIBRARY_UNAVAILABLE_RESPONSE,
    },
)
async def restore_trash(request: Request, body: RestoreRequest) -> RestoreResult:
    """Put a trashed folder back. 409 if busy, 404 if not in Trash, 503 if it cannot be moved."""
    app = request.app
    _gate(app)
    async with _swap_lock(app):
        checked, dest = _child_or_404(app, body.folder)
        try:
            result = await run_in_threadpool(
                restore_album,
                checked.handle.lib,
                str(dest),
                trash_dir=checked.trash_dir,
                origins_dir=checked.origins_dir,
                protected=_required(checked.protected),
            )
            emit_library_changed(app)
            return result
        # Ahead of the blanket 500, and for the same reason delete_album_op puts
        # it there: this guard fires BEFORE anything leaves Trash, so "the
        # restore failed" would be true but useless while "the share is not
        # mounted" is actionable. Raised inline so the status stays a literal
        # tests/test_route_status_declarations.py can see.
        except LibraryRootUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # The same tier for the same reason: restore is a mover, the guard fires
        # before anything leaves Trash, and the fix is the operator's.
        except ProtectedTreeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # The third guard of that tier, and the 503 it reuses is deliberate: the
        # declared description ("the folder was not moved out of Trash; the
        # message says which setup fault refused it") is true of it word for
        # word, so this adds no status and no OpenAPI change. A 409 would have
        # cost one, since that description enumerates its two causes.
        except TrashEntryUnreadableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # 409, not 503: this is the "a library operation is in progress" cause
        # the declared conflict description already names, and it is transient
        # in the caller's own terms -- an import owns the beets import config
        # until its review finishes. Reaching it needs the check-then-act
        # window ``library_busy`` documents against itself; before the refusal
        # existed this thread blocked here for the length of that review while
        # holding the swap lock, 409-ing every library route with no cause
        # given. Adds no status and no OpenAPI change.
        except ImportConfigBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc


@router.delete(
    "/trash",
    responses={
        409: _TRASH_CONFLICT_RESPONSE,
        404: _TRASH_NOT_FOUND_RESPONSE,
        500: _TRASH_EMPTY_FAILED_RESPONSE,
        503: _TRASH_LAYOUT_REFUSED_RESPONSE,
    },
)
async def empty_trash_one(request: Request, folder: Annotated[str, Query()]) -> EmptyResult:
    """Permanently remove one trashed album folder. 409 if busy, 404 if not in Trash."""
    app = request.app
    _gate(app)
    async with _swap_lock(app):
        # Inside the lock, and ONE check: the path that gets ``rmtree``'d is
        # derived from the pair that check approved. It used to resolve the
        # child outside the lock from a first check and re-check inside, so the
        # pair that was validated and the path acted on came from two instants.
        checked, dest = _child_or_404(app, folder)
        try:
            result = await run_in_threadpool(
                empty_one,
                str(dest),
                origins_dir=checked.origins_dir,
                protected=_required(checked.protected),
                # The library, so an entry whose files the library still names is
                # refused: after a delete whose row drop raised, that entry is
                # the album's ONLY copy and one click destroyed it (measured).
                lib=checked.handle.lib,
            )
        # 503, like the layout refusal it completes: the entry is still in Trash
        # and the fix is the operator's. Raised inline so the status stays a
        # literal tests/test_route_status_declarations.py can see.
        except ProtectedTreeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # A mode-000 entry raises out of the removal. The sweep names such an
        # entry and answers 500; this route used to let the OSError fall into
        # the blanket 500, which is the same status with no declared body.
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Empty Trash: {exc}") from exc
        emit_library_changed(app)
    return result


@router.delete(
    "/trash/all",
    responses={
        # NOT ``_TRASH_CONFLICT_RESPONSE``: this route never calls
        # ``_child_or_404``, so the ambiguous-name arm of that sentence cannot
        # happen here. Only the shared gate can refuse.
        409: {
            "model": ErrorDetail,
            "description": (
                "The operation was refused because a library operation is in"
                " progress or the beets swap lock is held."
            ),
        },
        500: _TRASH_EMPTY_PARTIAL_RESPONSE,
        503: _TRASH_LAYOUT_REFUSED_RESPONSE,
    },
)
async def empty_trash_all(request: Request) -> EmptyResult:
    """Permanently clear the whole Trash dir. 409 if busy, 500 if partly cleared."""
    app = request.app
    _gate(app)
    async with _swap_lock(app):
        # Inside the lock, so a config Apply cannot swap the handle between the
        # check and the rmtree.
        checked = _store(app, protected=True)
        try:
            result = await run_in_threadpool(
                empty_all,
                checked.trash_dir,
                origins_dir=checked.origins_dir,
                protected=_required(checked.protected),
                lib=checked.handle.lib,  # see empty_trash_one
            )
        # A partial sweep still CHANGED the library, so the event fires before
        # the error propagates — the page must not keep showing entries that are
        # now gone just because the ones after them could not be removed.
        except TrashEmptyPartialError as exc:
            emit_library_changed(app)
            raise HTTPException(status_code=500, detail=f"Empty Trash: {exc}") from exc
        # Entries the guard left behind. Same event-first reason as the partial
        # above — the ones this call DID remove are gone from the page.
        except ProtectedTreeError as exc:
            emit_library_changed(app)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # The twin of ``empty_trash_one``'s arm: the root open, or an origin
        # record that could not be dropped after its entry went, raises out of
        # here and used to fall into the blanket 500 with no declared body. The
        # event fires first for the same reason the two above it do — entries
        # this call already removed are gone from the page.
        except OSError as exc:
            emit_library_changed(app)
            raise HTTPException(status_code=500, detail=f"Empty Trash: {exc}") from exc
        emit_library_changed(app)
    return result
