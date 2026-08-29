# backend/app/api/disk_sync.py
"""Sync-with-disk endpoints: dry-run preview + the single-slot job.

Library-wide only. Mutually exclusive with every other library writer (the
shared ``app.library_busy`` union — see _gate_busy) in BOTH directions."""

from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.beets.disk_sync import plan_disk_sync
from app.beets.library import LibraryHandle, LibraryRootUnavailableError
from app.disk_sync_jobs.registry import (
    DiskSyncRegistry,
    get_disk_sync_registry,
)
from app.disk_sync_jobs.runner import start_backfill
from app.events.emit import emit_library_changed
from app.library_busy import raise_if_library_busy
from app.models.disk_sync import DiskSyncPlan, DiskSyncStatus
from app.models.errors import ErrorDetail
from app.playlists.store import get_playlists_dir

router = APIRouter(tags=["disk-sync"])

_BUSY = "A library operation is in progress; disk sync available when it finishes"

#: Both statuses below are spelled ``status.HTTP_*``, which is exactly why
#: SonarQube python:S8415 never flagged this file - the rule reads integer
#: literals only. Each entry names a model so the body keeps a generated type
#: (see app/models/errors.py).
_DISK_SYNC_BUSY_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "A disk sync is already running, or another library job or a beets swap holds the library."
    ),
}


def _gate_busy(app: object) -> None:
    # Excludes disk_sync's own slot — this is disk sync's start-gate; the
    # single-slot check stays at ``reg.start`` (RuntimeError -> 409).
    raise_if_library_busy(app, exclude=("disk_sync",), message=_BUSY)


@router.get(
    "/disk-sync/preview",
    responses={
        503: {
            "model": ErrorDetail,
            "description": (
                "The music library root is missing, empty or unreadable, so no"
                " plan is computed (the guard against an unmounted share)."
            ),
        },
    },
)
async def preview_disk_sync(
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> DiskSyncPlan:
    """Dry run: what a sync would remove/update. Read-only."""
    try:
        return await run_in_threadpool(plan_disk_sync, handle.lib)
    except LibraryRootUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.post("/disk-sync", responses={409: _DISK_SYNC_BUSY_RESPONSE})
async def start_disk_sync(
    request: Request,
    reg: Annotated[DiskSyncRegistry, Depends(get_disk_sync_registry)],
    # Depends (not a direct call) so app.dependency_overrides reaches this route
    # too — a bare get_playlists_dir() here would hand the WORKER the real
    # settings-derived store while every test override silently misses it.
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> DiskSyncStatus:
    _gate_busy(request.app)
    try:
        reg.start()
    except RuntimeError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A disk sync is already running") from None
    app = request.app
    handle = app.state.beets_library
    start_backfill(
        reg,
        handle,
        playlists_dir=playlists_dir,
        on_complete=lambda: emit_library_changed(app),
    )
    return reg.state()


@router.get("/disk-sync/status")
async def disk_sync_status(
    reg: Annotated[DiskSyncRegistry, Depends(get_disk_sync_registry)],
) -> DiskSyncStatus:
    return reg.state()


@router.post("/disk-sync/stop")
async def stop_disk_sync(
    reg: Annotated[DiskSyncRegistry, Depends(get_disk_sync_registry)],
) -> DiskSyncStatus:
    reg.request_stop()
    return reg.state()
