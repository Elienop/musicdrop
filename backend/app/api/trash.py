"""Trash management API: list / restore / empty (Settings → Trash).

Mirrors the delete op's mutual exclusion: restore runs an import and empty races
the import's Replace-to-trash pass, so both refuse (409) while any library job is
active; restore additionally holds the beets swap lock across the synchronous
re-import. Every folder argument flows through ``resolve_trash_child`` (404 on
traversal) — these are rm -rf / import targets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

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
from app.library_busy import library_job_active
from app.models.trash import EmptyResult, RestoreRequest, RestoreResult, TrashListing

router = APIRouter(tags=["trash"])


def _gate() -> None:
    """Refuse (409) while any library-mutating job runs (mirrors delete._gate)."""
    if library_job_active():
        raise HTTPException(
            status_code=409,
            detail="A library operation is in progress; try again when it finishes",
        )


def _child_or_404(app: Any, folder: str) -> tuple[LibraryHandle, Path]:
    handle: LibraryHandle = app.state.beets_library
    trash_dir = resolve_trash_dir(_settings(app), handle)
    try:
        return handle, resolve_trash_child(trash_dir, folder)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not in Trash") from None


@router.get("/trash", response_model=TrashListing)
async def list_trash(request: Request) -> TrashListing:
    """List the albums sitting in Trash (read off disk; no gate)."""
    app = request.app
    handle: LibraryHandle = app.state.beets_library
    trash_dir = resolve_trash_dir(_settings(app), handle)
    albums = await run_in_threadpool(list_trashed_albums, trash_dir)
    return TrashListing(albums=albums, trash_path=str(trash_dir))


@router.post("/trash/restore", response_model=RestoreResult)
async def restore_trash(request: Request, body: RestoreRequest) -> RestoreResult:
    """Re-import a trashed folder as-is. 409 if busy, 404 if not in Trash."""
    app = request.app
    _gate()
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


@router.delete("/trash", response_model=EmptyResult)
async def empty_trash_one(request: Request, folder: Annotated[str, Query()]) -> EmptyResult:
    """Permanently remove one trashed album folder. 409 if busy, 404 if not in Trash."""
    _gate()
    _handle, dest = _child_or_404(request.app, folder)
    result = await run_in_threadpool(empty_one, str(dest))
    emit_library_changed(request.app)
    return result


@router.delete("/trash/all", response_model=EmptyResult)
async def empty_trash_all(request: Request) -> EmptyResult:
    """Permanently clear the whole Trash dir. 409 if busy."""
    app = request.app
    _gate()
    handle: LibraryHandle = app.state.beets_library
    trash_dir = resolve_trash_dir(_settings(app), handle)
    result = await run_in_threadpool(empty_all, trash_dir)
    emit_library_changed(app)
    return result
