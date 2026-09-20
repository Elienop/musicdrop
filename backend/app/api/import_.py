"""The import API router (chunk 2, sequential review).

Thin endpoints over the in-memory ImportJobRegistry. Start an import, poll its
phase + live feed, fetch the parked album's full Candidate, and push its choice.
beets imports one album at a time, so there is no batch apply-ready route. The
registry owns all lifecycle + threading; this layer validates input and maps
registry/bridge exceptions to HTTP codes.

No beets imports: the registry + models are the whole surface here.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated, Final, TypeVar

import anyio
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
from app.import_jobs.runner import InLibraryCopyError, SourcePathMissingError
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
#: item rows (an unmounted share) - or by a start that waited out
#: ``app/api/import_.py::_START_WAIT_SECONDS`` for the one start token.
#: The two copies of this entry (here and the other import-starting router) must
#: stay worded alike; the OpenAPI schema carries whichever the route declares.
_LIBRARY_REFUSED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "The store layout is refused, the library folder is unavailable, or"
        " another import is still starting."
    ),
}
#: ``reg.candidate`` / ``parked_album`` raise KeyError for BOTH an unknown job
#: and an index with nothing parked on it, and the route cannot tell them apart.
_PARKED_ALBUM_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "No import job has that id, no album is parked at that index, or the import has finished."
    ),
}
_PARKED_DUPLICATE_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "No import job has that id, no duplicate is parked at that index, or the import"
        " has finished."
    ),
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


_T = TypeVar("_T")

#: How many import STARTS may occupy anyio's worker pool at once.
#:
#: That pool is PROCESS-WIDE (40 tokens, anyio 4.13.0) and shared with every
#: sync ``Depends`` and the scrypt derive behind sign-in. A start stats the
#: caller's path, and a stat on a hung mount does not return: 40 concurrent
#: starts against one took every token and ``POST /api/auth/login`` timed out at
#: 20 s while ``GET /api/health`` still answered in 0.7 ms (measured).
_IMPORT_START_SLOTS: Final = anyio.CapacityLimiter(1)

#: How long a start may wait for that one token before it refuses.
#:
#: Bounded, because "the single import slot already 409s a concurrent start" is
#: false in exactly the case this limiter exists for: ``runner.validate`` runs
#: in ``ImportJobRegistry.start`` BEFORE its ``claim_slot``, so a start wedged
#: in ``validate``'s ``os.stat`` holds this token while ``has_active_job()``
#: still reads idle.
#:
#: 30 s is an order of magnitude above a legitimate start (one ``os.stat`` per
#: handed-over folder plus one library-root read - ~2 s for 200 folders at a
#: pessimistic 10 ms per cold remote stat), so a working share is never refused
#: while a mount that never answers stops being an infinite wait. It is NOT a
#: per-caller guarantee: anyio's limiter is strict FIFO with no barging, so a
#: waiter's deadline covers everyone ahead of it too, and enough queued
#: max-length posts push the tail caller past it on a healthy share. That is a
#: refusal under load rather than under a fault, which is what the 503 says.
_START_WAIT_SECONDS: Final = 30.0

#: Short and human: the operator cannot tell a wedged start from a slow one, so
#: the sentence says what to do rather than what happened.
_START_BUSY_DETAIL: Final = (
    "Another import is still starting. Try again in a moment, or check that your"
    " music share is responding."
)


async def start_import_off_loop(start: Callable[[], _T]) -> _T:
    """Run ONE import start on a worker thread, under ``import_start_admission``.

    The token is held until the WORKER RETURNS, not until the caller gives up,
    because ``run_in_threadpool`` defaults ``abandon_on_cancel=False``. That
    shield is anyio's and only anyio's own scopes honour it; no caller can reach
    this any other way today (every one is a FastAPI handler, and Starlette
    awaits handlers directly - no disconnect watcher, no task group).
    """
    async with import_start_admission():
        return await run_in_threadpool(start)


@asynccontextmanager
async def import_start_admission() -> AsyncIterator[None]:
    """Hold the one start token across EVERY blocking hop of a start handler.

    A context manager and not just ``start_import_off_loop`` because a route is
    bounded by its FIRST unbounded blocking hop: ``start_import`` resolves the
    posted path on a worker thread BEFORE it starts anything, so capping only
    the start left N concurrent callers holding N tokens in the resolve.

    Admission is taken BY HAND rather than passed to anyio as ``limiter=``,
    which would let a cancelled caller release its token while the worker thread
    is still stuck.
    """
    admitted = False
    with anyio.move_on_after(_START_WAIT_SECONDS):
        await _IMPORT_START_SLOTS.acquire()
        # ``admitted`` and not ``scope.cancel_called``: the token can be handed
        # over just as the deadline fires, and only this flag says whether there
        # is one to release. No await between the acquire and this line.
        admitted = True
    if not admitted:
        raise HTTPException(status_code=503, detail=_START_BUSY_DETAIL)
    try:
        yield
    finally:
        _IMPORT_START_SLOTS.release()


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
        # Two refusals of a well-formed request the importer declines on its
        # merits, so they stay 422 - which means this route returns BOTH 422
        # bodies (see app/models/errors.py).
        422: validation_or_detail_422(
            "The source folder does not exist or cannot be read, or a copy-mode"
            " import was asked for a folder inside the music library, or the"
            " request failed validation."
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
    # ONE admission across BOTH blocking hops. A route is bounded by its FIRST
    # unbounded hop, and the resolve below is a worker-thread hop too, so capping
    # only the start left N concurrent callers holding N of anyio's 40 tokens in
    # the resolve. Under one admission the whole handler costs ONE.
    #
    # NEVER NEST THESE. Inside this block the start goes through a bare
    # ``run_in_threadpool``, not ``start_import_off_loop``, because that helper
    # takes the SAME 1-token limiter: a second acquire by the same borrower
    # raises ``RuntimeError("this borrower is already holding one of this
    # CapacityLimiter's tokens")``, which the ``except RuntimeError`` below would
    # turn into a 409 "An import is already running" on an idle app. Already
    # pinned, so no new test: swapping this call for ``start_import_off_loop``
    # turns 20 of tests/test_import_start_guards.py's 76 red, the plain
    # "still starts" cases among them (measured 2026-09-20).
    async with import_start_admission():
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
            # ``resolve_display_path``). Two of them (``POST /api/trash/restore``,
            # ``DELETE /api/trash``) still resolve on the loop; the inbox per-item
            # import moved off it on 2026-09-20 together with its start.
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
            # ``reg.start`` -> ``runner.validate`` stats the caller's path, and a stat on
            # a hung mount does not return: on a FUSE filesystem whose ``getattr`` sleeps,
            # ``start`` took 10000.6 ms and the event loop served nobody for 10002.7 ms
            # against a 2.1 ms idle baseline (security seat, measured 2026-09-20). Off the
            # loop it genuinely goes away rather than shrinking — ``os.stat`` releases the
            # GIL, unlike the pure-Python resolve above.
            #
            # Safe on a worker thread because two daemon threads already call it
            # (``AcquisitionQueue._process_one``, ``BankApplyRunner._apply_one``); every
            # lock it TAKES is a ``threading`` one, and the one it READS is an
            # ``asyncio.Lock`` it only asks ``locked()`` of
            # (``claim_slot`` -> ``library_busy._swap_in_progress``, the lock created at
            # app/main.py:231) — on CPython 3.12.13 that method is ``return self._locked``
            # and touches no loop, which is what makes the read safe rather than the lock's
            # type. An ``await lock.acquire()`` in that place would not be.
            # ``claim_slot`` is entered AFTER ``validate`` returns and is held only across
            # the O(1) check+claim, so a stuck stat cannot wedge the gate for the other job
            # types.
            #
            # The trade-off: a hung mount holds a worker thread for as long as it hangs,
            # instead of the loop. The ``import_start_admission`` above bounds THIS
            # HANDLER - both hops, not just this one - at ONE of anyio's 40
            # process-wide tokens, shared with every sync ``Depends`` and the sign-in
            # derive, so the rest of the app keeps 39 however many callers arrive.
            job_id = await run_in_threadpool(partial(reg.start, path, options=body.options))
        except (SourcePathMissingError, InLibraryCopyError) as exc:
            # Guard refusals (validated before any slot was taken): actionable 422.
            # Kept as two types so a caller can tell the missing source from the
            # in-library copy; the status and the body shape are the same.
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
                "No import job has that id, no album is parked at that index, the import"
                " has finished, or the parked album has no embedded cover art."
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
