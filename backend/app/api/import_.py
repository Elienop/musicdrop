"""The import API router (chunk 2, sequential review).

Thin endpoints over the in-memory ImportJobRegistry. Start an import, poll its
phase + live feed, fetch the parked album's full Candidate, and push its choice.
beets imports one album at a time, so there is no batch apply-ready route. The
registry owns all lifecycle + threading; this layer validates input and maps
registry/bridge exceptions to HTTP codes.

No beets imports: the registry + models are the whole surface here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.import_jobs.registry import ImportJobRegistry, get_registry
from app.models.import_api import (
    ImportJobState,
    StartImportRequest,
    StartImportResponse,
)
from app.models.import_models import Candidate, ImportChoice

router = APIRouter(tags=["import"])


@router.post(
    "/import", response_model=StartImportResponse, status_code=status.HTTP_202_ACCEPTED
)
async def start_import(
    body: StartImportRequest,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> StartImportResponse:
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


@router.post(
    "/import/{job_id}/albums/{index}/choice", status_code=status.HTTP_204_NO_CONTENT
)
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
