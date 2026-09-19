"""The import API router (chunk 2, sequential review).

Thin endpoints over the in-memory ImportJobRegistry. Start an import, poll its
phase + live feed, fetch the parked album's full Candidate, and push its choice.
beets imports one album at a time, so there is no batch apply-ready route. The
registry owns all lifecycle + threading; this layer validates input and maps
registry/bridge exceptions to HTTP codes.

No beets imports: the registry + models are the whole surface here.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from app.api.candidate_identity import selected_option_identity
from app.api.http_cache import NO_SNIFF
from app.artwork.images import FALLBACK_CONTENT_TYPE, header_safe_content_type
from app.beets.duplicates import find_import_duplicates
from app.beets.library import LibraryHandle, LibraryRootUnavailableError
from app.import_jobs.registry import (
    ImportJobRegistry,
    LibraryRefusedError,
    get_registry,
)
from app.import_jobs.runner import InLibraryCopyError
from app.models.errors import ErrorDetail, validation_or_detail_422
from app.models.import_api import (
    ActiveImportStatus,
    ImportJobState,
    StartImportRequest,
    StartImportResponse,
)
from app.models.import_models import (
    Candidate,
    DuplicateDecision,
    DuplicatePrompt,
    DuplicatesCheckResponse,
    ImportChoice,
)
from app.wire import AmbiguousDisplayName, resolve_posted_path

_IMPORT_ALBUM_NOT_FOUND = "Import album not found"
#: The stop route's 409 body and the description its ``responses=`` block declares.
_IMPORT_NOT_RUNNING = "That import is no longer running."

router = APIRouter(tags=["import"])

#: The OpenAPI entries this router's routes share. Every one carries a NAMED
#: model: a description-only entry REPLACES FastAPI's generated response and
#: drops the ``content`` block, so openapi-typescript emits ``content?: never``
#: for a body the import screen actually reads (see app/models/errors.py).
#:
#: These statuses were invisible to SonarQube python:S8415 because they are
#: spelled ``status.HTTP_404_NOT_FOUND`` rather than ``404`` - the rule only
#: matches integer literals. tests/test_route_status_declarations.py resolves
#: both spellings and is what keeps this list honest.
_JOB_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "No import job has that id.",
}
#: Set by Apply's backstop when beets loaded a layout the rule refuses, or by the
#: music root being missing or unreadable, or empty while the library holds
#: item rows (an unmounted share).
_LIBRARY_REFUSED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "The store layout is refused or the library folder is unavailable.",
}
#: ``reg.candidate`` / ``parked_album`` raise KeyError for BOTH an unknown job
#: and an index with nothing parked on it, and the route cannot tell them apart.
_PARKED_ALBUM_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "No import job has that id, or no album is parked at that index.",
}
_PARKED_DUPLICATE_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "No import job has that id, or no duplicate is parked at that index.",
}


@router.get("/imports/active")
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
    reorganize) or a disk sync holds the slot. Best-effort `asyncio.Lock.locked()`, the same
    single-user TOCTOU posture as config_editor.apply's import gate.
    """
    lock = getattr(request.app.state, "beets_swap_lock", None)
    if lock is not None and lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A library operation is in progress; import available when it finishes",
        )

    from app.library_busy import library_job_active

    # Excludes the import slot — that single-slot policy is enforced at
    # ``reg.start`` (RuntimeError -> 409); here we only refuse for the OTHER jobs.
    if library_job_active(exclude=("import",)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A library backfill is in progress; import available when it finishes",
        )


@router.post(
    "/import",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        # Three distinct refusals, one status: the swap-lock arm and the
        # backfill arm of ``ensure_import_can_start`` above, plus the registry's
        # single-slot RuntimeError below. The Import screen branches on this
        # status (frontend/src/api/useImport.ts) to re-poll the active-import
        # probe and offer Resume, so it needs a typed body.
        409: {
            "model": ErrorDetail,
            "description": (
                "An import is already running, or a beets swap (such as a config Apply or"
                " duplicate resolve) or a lyrics backfill, an artist-art backfill, a"
                " reorganize backfill, or a disk sync holds the library, or two folders"
                " display under the same name."
            ),
        },
        # The copy-in-library refusal is a well-formed request the importer
        # declines on its merits, so it stays 422 - which means this route
        # returns BOTH 422 bodies (see app/models/errors.py).
        422: validation_or_detail_422(
            "A copy-mode import was asked for a folder inside the music library,"
            " or the request failed validation."
        ),
        503: _LIBRARY_REFUSED_RESPONSE,
    },
)
async def start_import(
    body: StartImportRequest,
    request: Request,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> StartImportResponse:
    ensure_import_can_start(request)
    # The posted path is the one the app DISPLAYED — a job's ``path`` re-posted
    # by "Import them again", or the folder the album page names — and the wire
    # scrub replaced any byte UTF-8 cannot carry. Map it back before beets sees
    # it; the ordinary path is returned untouched.
    try:
        # Off the loop reduces the stall; it does not remove it. The dominant
        # cost is pure-Python ``pathlib``/``posixpath.join`` work, which holds
        # the GIL — ``os.scandir`` profiled at ~0.5% of it. At the
        # 4096-character cap (2048 placeholder components) the request costs
        # ~331 ms and the loop serves nobody for 175-334 ms of that, across five
        # runs with a 2 ms poller (security seat, measured 2026-09-19). The
        # three sibling routes take a RELATIVE PATH too — 255 characters admit
        # 128 components (``"x/" * 127 + "x"``, counted through
        # ``resolve_display_path``) — and stay on the loop.
        path = await run_in_threadpool(resolve_posted_path, body.path)
    except AmbiguousDisplayName:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Two folders display under the same name because their names are "
                "not valid UTF-8. Rename one on disk to tell them apart."
            ),
        ) from None
    try:
        job_id = reg.start(path, options=body.options)
    except InLibraryCopyError as exc:
        # Guard refusal (validated before any slot was taken): actionable 422.
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except (LibraryRefusedError, LibraryRootUnavailableError) as exc:
        # Apply loaded a refused layout, or the music share is not there: an
        # import would write into a root the app refuses to file into. The
        # refused-layout arm is a RuntimeError, so it stays ahead of that one.
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except RuntimeError:
        # An import is already running (single-slot policy).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An import is already running"
        ) from None
    return StartImportResponse(job_id=job_id)


@router.get("/import/{job_id}", responses={404: _JOB_NOT_FOUND_RESPONSE})
async def get_import_state(
    job_id: str, reg: Annotated[ImportJobRegistry, Depends(get_registry)]
) -> ImportJobState:
    try:
        return reg.state(job_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found"
        ) from None


@router.get(
    "/import/{job_id}/albums/{index}",
    responses={404: _PARKED_ALBUM_NOT_FOUND_RESPONSE},
)
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
            status_code=status.HTTP_404_NOT_FOUND, detail=_IMPORT_ALBUM_NOT_FOUND
        ) from None


@router.get(
    "/import/{job_id}/albums/{index}/cover",
    responses={
        # Three arms end here, not two: the registry's KeyError (unknown job, or
        # nothing parked at the index) AND a parked album whose first file
        # carries no embedded picture, which returns None rather than raising.
        404: {
            "model": ErrorDetail,
            "description": (
                "No import job has that id, no album is parked at that index, or"
                " the parked album has no embedded cover art."
            ),
        },
    },
)
async def get_import_album_cover(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> Response:
    try:
        # candidate_cover parses the parked album's first audio file for embedded
        # art (HDD/NAS source) — offload so the read never stalls the event loop.
        # It takes the registry lock internally and reads the file outside it, so
        # it is threadpool-safe.
        cover = await run_in_threadpool(reg.candidate_cover, job_id, index)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_IMPORT_ALBUM_NOT_FOUND
        ) from None
    if cover is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No cover art") from None
    image_bytes, mime = cover
    return Response(
        content=image_bytes,
        # Same provenance as the album-cover sink: a media file's embedded
        # picture MIME. Safe today (mediafile re-derives it from magic bytes),
        # guarded anyway: an external content-type reaching a response header
        # must be guarded, and a guard with one site missing is not a guard.
        # A comment claiming completeness is how the next reviewer stops looking.
        media_type=header_safe_content_type(mime) or FALLBACK_CONTENT_TYPE,
        # The only image response in the app that reaches neither the http_cache
        # constructors nor the artwork routes -
        # so nosniff is spelled out here too. A backstop that covers every image
        # response except one is not a backstop.
        headers={**NO_SNIFF, "Cache-Control": "no-store"},  # parked-album art is transient
    )


@router.get(
    "/import/{job_id}/albums/{index}/duplicates",
    responses={404: _PARKED_ALBUM_NOT_FOUND_RESPONSE},
)
async def get_import_album_duplicates(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    request: Request,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
    candidate_index: Annotated[int, Query(ge=0)] = 0,
) -> DuplicatesCheckResponse:
    """Up-front library-collision check for the SELECTED candidate option.

    A heads-up only — Apply still routes through beets' duplicate prompt.
    Pure library read keyed on the option's own identity (the bank check's
    exact posture); never touches the parked worker.
    """
    try:
        parked = reg.parked_album(job_id, index)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_IMPORT_ALBUM_NOT_FOUND
        ) from None
    candidate = parked.candidate
    albumartist, album, year, mb_albumid = selected_option_identity(
        candidate.options, candidate_index, candidate.album_after
    )
    handle: LibraryHandle = request.app.state.beets_library
    existing = await run_in_threadpool(
        find_import_duplicates,
        handle.lib,
        albumartist=albumartist,
        album=album,
        year=year,
        mb_albumid=mb_albumid,
        exclude_under=parked.folder,
    )
    return DuplicatesCheckResponse(existing=existing)


@router.post(
    "/import/{job_id}/albums/{index}/choice",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: _PARKED_ALBUM_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": "A choice was already submitted for the album at that index.",
        },
    },
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
            status_code=status.HTTP_404_NOT_FOUND, detail=_IMPORT_ALBUM_NOT_FOUND
        ) from None
    except RuntimeError:
        # A choice was already pushed for this album, racing the same slot.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A choice was already submitted"
        ) from None


@router.get(
    "/import/{job_id}/albums/{index}/duplicate",
    responses={404: _PARKED_DUPLICATE_NOT_FOUND_RESPONSE},
)
async def get_import_duplicate(
    job_id: str,
    index: Annotated[int, Path(ge=0)],
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> DuplicatePrompt:
    try:
        return reg.parked_duplicate(job_id, index)
    except KeyError:
        # Unknown job, no duplicate parked at this index, or the job is over.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No duplicate to resolve"
        ) from None


@router.post(
    "/import/{job_id}/albums/{index}/duplicate",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: _PARKED_DUPLICATE_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": "A decision was already submitted for the duplicate at that index.",
        },
    },
)
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


@router.post(
    "/import/{job_id}/stop",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: _JOB_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": _IMPORT_NOT_RUNNING,
        },
    },
)
async def stop_import(
    job_id: str, reg: Annotated[ImportJobRegistry, Depends(get_registry)]
) -> None:
    """Stop the active import at the album it is on; what already landed stays."""
    # Repeating a stop on an active job is idempotent (204). The 409 detail is
    # the literal the responses= block declares, like every sibling route: a
    # str(exc) here would publish whatever the next raise under request_stop says.
    try:
        reg.request_stop(job_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found"
        ) from None
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_IMPORT_NOT_RUNNING
        ) from None
