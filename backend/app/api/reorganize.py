# backend/app/api/reorganize.py
"""Reorganize Library endpoints: dry-run preview + the single-slot move job.

Scope is carried by route + ``?artist=`` query (library = no query; artist =
?artist=NAME; album = nested under /albums/{id}). One global registry serves all
three — only one reorganize at a time — and it is mutually exclusive with every
other library write (see _gate_busy + the gate sites in edit/cover/config/
duplicates/import/lyrics/artists)."""

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.beets.config_editor import _settings
from app.beets.library import LibraryHandle, album_exists
from app.beets.reorganize import album_scope_label, plan_reorganize
from app.beets.trash import resolve_trash_dir
from app.events.emit import emit_library_changed
from app.library_busy import raise_if_library_busy
from app.models.reorganize import ReorganizeBackfillStatus, ReorganizePlan, ReorganizeScope
from app.reorganize_jobs.registry import (
    ReorganizeRegistry,
    get_reorganize_backfill,
)
from app.reorganize_jobs.runner import start_backfill

router = APIRouter(tags=["reorganize"])

_BUSY = "A library operation is in progress; reorganize available when it finishes"


def _gate_busy(app: object) -> None:
    # Excludes reorganize's own slot — this is reorganize's start-gate; the
    # single-slot check stays at ``reg.start`` (RuntimeError -> 409).
    raise_if_library_busy(app, exclude=("reorganize",), message=_BUSY)


def _trash_dir(app: object) -> Path:
    """The configured Trash dir for the orphan sweep (resolved like the trash API)."""
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    return resolve_trash_dir(_settings(app), handle)  # type: ignore[arg-type]  # app duck-typed (object)


def _ignore_dirs(app: object) -> tuple[Path, ...]:
    """Dirs under the music root the orphan sweep must never trash — the resolved
    playlists export dir (defaults to <music>/.playlists). Dotdirs/NAS dirs are handled
    name-based in the scanner; this covers a configured non-dotfile export dir."""
    handle: LibraryHandle = app.state.beets_library  # type: ignore[attr-defined]  # app duck-typed (object)
    configured = _settings(app).playlists_export_dir.strip()  # type: ignore[arg-type]  # app duck-typed (object)
    export_dir = (
        Path(configured) if configured else Path(os.fsdecode(handle.lib.directory)) / ".playlists"
    )
    return (export_dir,)


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
        ignore_dirs=_ignore_dirs(request.app),
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
        ignore_dirs=_ignore_dirs(request.app),
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
        ignore_dirs=_ignore_dirs(app),
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
        ignore_dirs=_ignore_dirs(app),
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


@router.post("/reorganize/dismiss", response_model=ReorganizeBackfillStatus)
async def dismiss_reorganize(
    reg: Annotated[ReorganizeRegistry, Depends(get_reorganize_backfill)],
) -> ReorganizeBackfillStatus:
    """Clear a FINISHED job's result (its failure rows) from the slot.

    NO ``_gate_busy`` on purpose: this touches the in-memory registry only —
    never the library, never beets — so an import or another sweep running
    elsewhere has no reason to hold a stale error message on screen.

    Idempotent: dismissing an already-empty slot returns the idle status rather
    than 404. The caller is asking for "nothing displayed", and that is exactly
    what it gets; a 404 would make the UI special-case a state indistinguishable
    from success (a double click, a retry, or a concurrent tab that dismissed
    first). Returns the post-dismiss status so the caller can seed its cache
    without a follow-up GET.
    """
    try:
        reg.dismiss()
    except RuntimeError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A reorganize is running; stop it before dismissing"
        ) from None
    return reg.state()
