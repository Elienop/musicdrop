# backend/app/api/reorganize.py
"""Reorganize Library endpoints: dry-run preview + the single-slot move job.

Scope is carried by route + ``?artist=`` query (library = no query; artist =
?artist=NAME; album = nested under /albums/{id}). One global registry serves all
three — only one reorganize at a time — and it is mutually exclusive with every
other library write (see _gate_busy + the gate sites in edit/cover/config/
duplicates/import/lyrics/artists)."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.artist_art_jobs.registry import artist_art_backfill_active
from app.beets.config_editor import _settings
from app.beets.library import LibraryHandle, album_exists
from app.beets.reorganize import album_scope_label, plan_reorganize
from app.beets.trash import resolve_trash_dir
from app.events.emit import emit_library_changed
from app.import_jobs.registry import get_registry
from app.lyrics_jobs.registry import lyrics_backfill_active
from app.models.reorganize import ReorganizeBackfillStatus, ReorganizePlan, ReorganizeScope
from app.reorganize_jobs.registry import (
    ReorganizeRegistry,
    get_reorganize_backfill,
)
from app.reorganize_jobs.runner import start_backfill

router = APIRouter(tags=["reorganize"])

_BUSY = "A library operation is in progress — reorganize available when it finishes"


def _gate_busy(app: object) -> None:
    if get_registry().has_active_job() or lyrics_backfill_active() or artist_art_backfill_active():
        raise HTTPException(status.HTTP_409_CONFLICT, _BUSY)
    lock = getattr(app.state, "beets_swap_lock", None)  # type: ignore[attr-defined]  # app is duck-typed (object) so tests can pass a stub
    if lock is not None and lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, _BUSY)


def _trash_dir(app: object) -> Path:
    """The configured Trash dir for the orphan sweep (resolved like the trash API)."""
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    return resolve_trash_dir(_settings(app), handle)  # type: ignore[arg-type]  # app duck-typed (object)


@router.get("/reorganize/preview", response_model=ReorganizePlan)
async def preview_reorganize(
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
    artist: Annotated[str | None, Query(min_length=1)] = None,
) -> ReorganizePlan:
    """Dry run: what would move under the current path config. Read-only."""
    scope: ReorganizeScope = "artist" if artist is not None else "library"
    return await run_in_threadpool(
        plan_reorganize,
        handle.lib,
        scope=scope,
        artist=artist,
        album_id=None,
        trash_dir=_trash_dir(request.app),
    )


@router.get("/albums/{album_id}/reorganize/preview", response_model=ReorganizePlan)
async def preview_album_reorganize(
    album_id: int,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> ReorganizePlan:
    exists = await run_in_threadpool(album_exists, handle, album_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Album not found")
    return await run_in_threadpool(
        plan_reorganize,
        handle.lib,
        scope="album",
        artist=None,
        album_id=album_id,
        trash_dir=_trash_dir(request.app),
    )


@router.post("/reorganize", response_model=ReorganizeBackfillStatus)
async def start_reorganize(
    request: Request,
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
    artist: Annotated[str | None, Query(min_length=1)] = None,
) -> ReorganizeBackfillStatus:
    _gate_busy(request.app)
    scope: ReorganizeScope = "artist" if artist is not None else "library"
    label = artist if artist is not None else "library"
    try:
        reg.start(scope=scope, artist=artist, album_id=None, scope_label=label)
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A reorganize is already running") from None
    app = request.app
    handle = app.state.beets_library
    start_backfill(
        reg,
        handle,
        scope=scope,
        artist=artist,
        album_id=None,
        trash_dir=_trash_dir(app),
        on_complete=lambda: emit_library_changed(app),
    )
    return reg.state()


@router.post("/albums/{album_id}/reorganize", response_model=ReorganizeBackfillStatus)
async def start_album_reorganize(
    album_id: int,
    request: Request,
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
) -> ReorganizeBackfillStatus:
    handle = request.app.state.beets_library
    label = await run_in_threadpool(album_scope_label, handle, album_id)
    if label is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Album not found")
    _gate_busy(request.app)
    try:
        reg.start(scope="album", artist=None, album_id=album_id, scope_label=label)
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A reorganize is already running") from None
    app = request.app
    start_backfill(
        reg,
        handle,
        scope="album",
        artist=None,
        album_id=album_id,
        trash_dir=_trash_dir(app),
        on_complete=lambda: emit_library_changed(app),
    )
    return reg.state()


@router.get("/reorganize/status", response_model=ReorganizeBackfillStatus)
async def reorganize_status(
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
) -> ReorganizeBackfillStatus:
    return reg.state()


@router.post("/reorganize/stop", response_model=ReorganizeBackfillStatus)
async def stop_reorganize(
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
) -> ReorganizeBackfillStatus:
    reg.request_stop()
    return reg.state()
