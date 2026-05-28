"""The import API router (chunk 2, sequential review).

Thin endpoints over the in-memory ImportJobRegistry. Start an import, poll its
phase + live feed, fetch the parked album's full Candidate, and push its choice.
beets imports one album at a time, so there is no batch apply-ready route. The
registry owns all lifecycle + threading; this layer validates input and maps
registry/bridge exceptions to HTTP codes.

No beets imports: the registry + models are the whole surface here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status

from app.import_jobs.registry import ImportJobRegistry, get_registry
from app.models.import_api import (
    ActiveImportStatus,
    ImportJobState,
    StartImportRequest,
    StartImportResponse,
)
from app.models.import_models import Candidate, ImportChoice

router = APIRouter(tags=["import"])


@router.get("/imports/active", response_model=ActiveImportStatus)
async def get_active_import(
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> ActiveImportStatus:
    """Tiny probe the ``/settings`` page polls to gate the Apply button.

    Returns ``{"active": True}`` while any import is in flight (i.e. the
    registry's single slot is in ``_ACTIVE_PHASES``). ``POST /api/config/apply``
    409s in that case; the SettingsPage uses this poll to render an
    "Import in progress" state instead of letting the click race the gate.

    Plural path (``/imports/active``) to match the convention any future
    multi-import surface would adopt; the single-slot registry is an
    implementation detail.
    """
    return ActiveImportStatus(active=reg.has_active_job())


@router.post("/import", response_model=StartImportResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_import(
    body: StartImportRequest,
    request: Request,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> StartImportResponse:
    # Refuse while another in-process beets mutation holds the shared swap lock
    # (config Apply or duplicate resolve). beets' safety model is strictly
    # serial — never two threads in beets at once — and the importer spawns its
    # own worker thread that would otherwise race a resolve/Apply mutating the
    # same Library + SQLite. This closes the import-start direction of the
    # mutual-exclusion invariant (Apply/resolve already refuse while an import
    # is active); best-effort `asyncio.Lock.locked()`, the same single-user
    # TOCTOU posture as config_editor.apply's import gate.
    lock = getattr(request.app.state, "beets_swap_lock", None)
    if lock is not None and lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A library operation is in progress — import available when it finishes",
        )
    try:
        job_id = reg.start(body.path)
    except RuntimeError:
        # An import is already running (single-slot policy).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An import is already running"
        ) from None
    return StartImportResponse(job_id=job_id)


@router.get("/import/{job_id}", response_model=ImportJobState)
async def get_import_state(
    job_id: str, reg: Annotated[ImportJobRegistry, Depends(get_registry)]
) -> ImportJobState:
    try:
        return reg.state(job_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found"
        ) from None


@router.get("/import/{job_id}/albums/{index}", response_model=Candidate)
async def get_import_album(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> Candidate:
    try:
        return reg.candidate(job_id, index)
    except KeyError:
        # Either the job is unknown or no album is parked at this index.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import album not found"
        ) from None


@router.get("/import/{job_id}/albums/{index}/cover")
async def get_import_album_cover(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> Response:
    try:
        cover = reg.candidate_cover(job_id, index)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import album not found"
        ) from None
    if cover is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No cover art") from None
    image_bytes, mime = cover
    return Response(
        content=image_bytes,
        media_type=mime,
        headers={"Cache-Control": "no-store"},  # parked-album art is transient
    )


@router.post("/import/{job_id}/albums/{index}/choice", status_code=status.HTTP_204_NO_CONTENT)
async def post_import_choice(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    choice: ImportChoice,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> None:
    try:
        reg.record_choice(job_id, index, choice)
    except KeyError:
        # No job, or no album parked at this index (incl. a second choice after
        # the worker advanced — park popped the slot).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import album not found"
        ) from None
    except RuntimeError:
        # A choice was already pushed for this album, racing the same slot.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A choice was already submitted"
        ) from None
