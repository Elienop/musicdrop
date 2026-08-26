"""Trash management API: list / restore / empty (Settings → Trash).

Mirrors the delete op's mutual exclusion. Restore runs a move-import and empty
rm -rf's trashed folders, so the two must never touch the same tree at once: both
refuse (409) while any library job runs OR the beets swap lock is held, and both
hold that lock across their synchronous file work — so a restore and an empty (in
either order) serialize instead of racing. Every folder argument flows through
``resolve_trash_child`` (404 on traversal) — these are rm -rf / import targets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.beets.config_editor import _settings, _swap_lock
from app.beets.library import LibraryHandle
from app.beets.trash import resolve_trash_dir
from app.beets.trash_manage import (
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


def _gate(app: Any) -> None:
    """Refuse (409) while any library-mutating job runs OR the beets swap lock is
    held (mirrors delete._gate via the shared api-layer gate).

    The swap-lock arm is load-bearing here: ``restore_album`` runs its
    synchronous re-import under ``_swap_lock`` but never registers as a library
    job, so a job-only check let Empty-Trash ``rmtree`` the folder a live Restore
    was mid-move on — an irreversible loss ``raise_if_library_busy`` closes.
    """
    raise_if_library_busy(app)


def _child_or_404(app: Any, folder: str) -> tuple[LibraryHandle, Path]:
    handle: LibraryHandle = app.state.beets_library
    trash_dir = resolve_trash_dir(_settings(app), handle)
    try:
        return handle, resolve_trash_child(trash_dir, folder)
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


@router.get("/trash")
async def list_trash(request: Request) -> TrashListing:
    """List the albums sitting in Trash (read off disk; no gate)."""
    app = request.app
    handle: LibraryHandle = app.state.beets_library
    trash_dir = resolve_trash_dir(_settings(app), handle)
    albums = await run_in_threadpool(list_trashed_albums, trash_dir)
    return TrashListing(albums=albums, trash_path=str(trash_dir))


@router.post(
    "/trash/restore",
    responses={
        409: _TRASH_CONFLICT_RESPONSE,
        404: _TRASH_NOT_FOUND_RESPONSE,
        500: _TRASH_RESTORE_FAILED_RESPONSE,
    },
)
async def restore_trash(request: Request, body: RestoreRequest) -> RestoreResult:
    """Re-import a trashed folder as-is. 409 if busy, 404 if not in Trash."""
    app = request.app
    _gate(app)
    async with _swap_lock(app):
        handle, dest = _child_or_404(app, body.folder)
        trash_dir = resolve_trash_dir(_settings(app), handle)
        try:
            result = await run_in_threadpool(
                restore_album, handle.lib, str(dest), trash_dir=trash_dir
            )
            emit_library_changed(app)
            return result
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Restore failed: {exc}") from exc


@router.delete(
    "/trash",
    responses={409: _TRASH_CONFLICT_RESPONSE, 404: _TRASH_NOT_FOUND_RESPONSE},
)
async def empty_trash_one(request: Request, folder: Annotated[str, Query()]) -> EmptyResult:
    """Permanently remove one trashed album folder. 409 if busy, 404 if not in Trash."""
    app = request.app
    _gate(app)
    _handle, dest = _child_or_404(app, folder)
    async with _swap_lock(app):
        result = await run_in_threadpool(empty_one, str(dest))
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
    },
)
async def empty_trash_all(request: Request) -> EmptyResult:
    """Permanently clear the whole Trash dir. 409 if busy."""
    app = request.app
    _gate(app)
    handle: LibraryHandle = app.state.beets_library
    trash_dir = resolve_trash_dir(_settings(app), handle)
    async with _swap_lock(app):
        result = await run_in_threadpool(empty_all, trash_dir)
        emit_library_changed(app)
    return result
