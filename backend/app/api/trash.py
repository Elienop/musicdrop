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
from app.beets.library import LibraryHandle, LibraryRootUnavailableError, _music_dir
from app.beets.protected import ProtectedTreeError, ProtectedTrees
from app.beets.store_layout import (
    StoreLayoutError,
    checked_protected_trees,
    checked_store_dirs,
)
from app.beets.trash_manage import (
    TrashEmptyPartialError,
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
#: this. One sentence for all three, and it says nothing about what was left:
#: ``DELETE /api/trash/all`` removes every unprotected entry BEFORE it refuses.
_TRASH_LAYOUT_REFUSED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "A store-layout or identity refusal; the message names the cause.",
}
_TRASH_EMPTY_PARTIAL_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "Some Trash entries were removed and others could not be; the message names"
        " which are still there."
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
    try:
        trash_dir, origins_dir = checked_store_dirs(settings, handle)
    except StoreLayoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    trees = (
        checked_protected_trees(settings, handle, trash_dir=trash_dir, origins_dir=origins_dir)
        if protected
        else None
    )
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


@router.get("/trash", responses={503: _TRASH_LAYOUT_REFUSED_RESPONSE})
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
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc


@router.delete(
    "/trash",
    responses={
        409: _TRASH_CONFLICT_RESPONSE,
        404: _TRASH_NOT_FOUND_RESPONSE,
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
            )
        # 503, like the layout refusal it completes: the entry is still in Trash
        # and the fix is the operator's. Raised inline so the status stays a
        # literal tests/test_route_status_declarations.py can see.
        except ProtectedTreeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
        emit_library_changed(app)
    return result
