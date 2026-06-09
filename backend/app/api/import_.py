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
from app.models.import_models import Candidate, DuplicateDecision, DuplicatePrompt, ImportChoice

router = APIRouter(tags=["import"])


@router.get("/imports/active", response_model=ActiveImportStatus)
async def get_active_import(
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> ActiveImportStatus:
    """Probe used two ways: the ``/settings`` Apply gate polls ``active``, and
    the import Start screen reads ``job_id`` to offer "Resume" back into a
    running import the user navigated away from.

    ``active`` is ``True`` exactly while the registry's single slot is in
    ``_ACTIVE_PHASES`` (``POST /api/config/apply`` 409s in that case); ``job_id``
    carries the resume target (``None`` when idle). The probe also surfaces the
    active import's ``origin`` (manual/inbox) and set-aside ``needs_review_count``
    so the Resume cue can flag an unattended inbox import. All come from one
    ``active_status()`` call so they can never disagree.
    """
    return reg.active_status()


def ensure_import_can_start(request: Request) -> None:
    """Raise 409 if a beets mutation or backfill currently blocks a new import.

    Shared by the manual ``POST /import`` and the inbox
    ``POST /acquisition/review-inbox`` so both refuse identically. The single-slot
    check stays at the ``reg.start`` call site (RuntimeError -> 409).

    Refuses while another in-process beets mutation holds the shared swap lock
    (config Apply or duplicate resolve): beets' safety model is strictly serial —
    never two threads in beets at once — and the importer spawns its own worker
    thread that would otherwise race a resolve/Apply mutating the same Library +
    SQLite. Also refuses while a library backfill (lyrics / artist-art /
    reorganize) holds the slot. Best-effort `asyncio.Lock.locked()`, the same
    single-user TOCTOU posture as config_editor.apply's import gate.
    """
    lock = getattr(request.app.state, "beets_swap_lock", None)
    if lock is not None and lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A library operation is in progress — import available when it finishes",
        )

    from app.artist_art_jobs.registry import artist_art_backfill_active
    from app.lyrics_jobs.registry import lyrics_backfill_active
    from app.reorganize_jobs.registry import reorganize_backfill_active

    if lyrics_backfill_active() or artist_art_backfill_active() or reorganize_backfill_active():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A library backfill is in progress — import available when it finishes",
        )


@router.post("/import", response_model=StartImportResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_import(
    body: StartImportRequest,
    request: Request,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> StartImportResponse:
    ensure_import_can_start(request)
    try:
        job_id = reg.start(body.path, options=body.options)
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


@router.get("/import/{job_id}/albums/{index}/duplicate", response_model=DuplicatePrompt)
async def get_import_duplicate(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> DuplicatePrompt:
    try:
        return reg.duplicate_prompt(job_id, index)
    except KeyError:
        # Unknown job, or no duplicate parked at this index.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No duplicate to resolve"
        ) from None


@router.post("/import/{job_id}/albums/{index}/duplicate", status_code=status.HTTP_204_NO_CONTENT)
async def post_import_duplicate_decision(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    decision: DuplicateDecision,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> None:
    try:
        reg.record_duplicate_decision(job_id, index, decision)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No duplicate to resolve"
        ) from None
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A decision was already submitted"
        ) from None
