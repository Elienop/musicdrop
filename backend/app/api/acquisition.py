"""The acquisition status probe + the one-click inbox review.

``GET /acquisition/status`` is a read-only probe over the lifespan-constructed
``AcquisitionQueue`` (informational only — the queue is not a mutex participant,
Option A). Under the lifespan-less test client there is no queue on
``app.state``, so it falls back to an idle status rather than 500.

``POST /acquisition/review-inbox`` is the slskd-panel one-click review: it
resolves the fixed inbox path SERVER-SIDE (never sent to the browser) and starts
a normal *attended* import with ``operation="move"`` so applied albums leave the
inbox — targeting the SETTLED top-level folders, never the inbox root (which is
the downloader's live output dir). Nothing settled is a no-op (``started=False``),
never an error.
"""

from __future__ import annotations

import time
from functools import partial
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from app.acquisition.inbox import contain, count_pending, list_inbox, settled_folders
from app.acquisition.ledger import AcquisitionLedger
from app.api.import_ import ensure_import_can_start
from app.beets.library import LibraryRootUnavailableError
from app.config import settings
from app.fsutil import is_dir
from app.import_jobs.registry import (
    ImportJobRegistry,
    LibraryRefusedError,
    get_registry,
)
from app.models.acquisition import (
    AcquisitionQueueStatus,
    ImportInboxItemRequest,
    InboxListing,
    ReviewInboxResponse,
)
from app.models.errors import ErrorDetail
from app.models.import_models import ImportOptions
from app.wire import AmbiguousDisplayName, resolve_display_path

router = APIRouter(tags=["acquisition"])

#: Both import-starting routes below refuse with the SAME 409 for the same two
#: reasons: ``ensure_import_can_start`` (a beets swap, a library backfill, or
#: a disk sync owns the library) and the registry's single-slot ``RuntimeError``
#: at ``reg.start``.
#: Declared with a named model because a description-only entry would drop the
#: ``content`` block - see app/models/errors.py.
#: Set by Apply's backstop when beets loaded a layout the rule refuses, or by the
#: music root being missing or unreadable, or empty while the library holds
#: item rows (an unmounted share).
_LIBRARY_REFUSED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "The store layout is refused or the library folder is unavailable.",
}
_IMPORT_SLOT_TAKEN_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "An import is already running, or a beets swap (such as a config Apply or"
        " duplicate resolve) or a lyrics backfill, an artist-art backfill, a"
        " reorganize backfill, or a disk sync holds the library."
    ),
}


@router.get("/acquisition/status")
async def get_acquisition_status(request: Request) -> AcquisitionQueueStatus:
    inbox_dir = getattr(request.app.state, "inbox_dir", None)
    inbox_pending = (
        await run_in_threadpool(count_pending, inbox_dir) if inbox_dir is not None else 0
    )
    queue = getattr(request.app.state, "acquisition_queue", None)
    if queue is None:
        return AcquisitionQueueStatus(
            phase="idle",
            queued=0,
            current=None,
            processed=0,
            set_aside=0,
            failed=0,
            error=None,
            inbox_pending=inbox_pending,
        )
    snapshot: AcquisitionQueueStatus = queue.status()
    snapshot.inbox_pending = inbox_pending
    return snapshot


@router.post(
    "/acquisition/review-inbox",
    responses={409: _IMPORT_SLOT_TAKEN_RESPONSE, 503: _LIBRARY_REFUSED_RESPONSE},
)
async def review_inbox(
    request: Request,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> ReviewInboxResponse:
    """Start an attended, move-mode import of the SETTLED inbox folders.

    One-click review of the set-aside backlog from the slskd panel: no path is
    typed and the absolute inbox path never leaves the server. Strong matches
    auto-apply (and move out of the inbox); uncertain ones park for review in the
    normal candidate-review screen. Nothing to import is a no-op
    (``started=False``), never an error — and the shared import-slot gate refuses
    (409) while another beets mutation or backfill owns the slot.

    The inbox ROOT is never the import target. It is the downloader's live output
    directory, so importing it would sweep in every folder still receiving files
    and file a PARTIAL album (whose remaining tracks then arrive and import again
    as a duplicate). Instead each top-level folder that has been quiet for
    ``inbox_settle_seconds`` is handed over as its own beets toppath; folders
    still being written are left for the next click.
    """
    ensure_import_can_start(request)
    inbox_dir: Path | None = getattr(request.app.state, "inbox_dir", None)
    if inbox_dir is None:
        return ReviewInboxResponse(started=False, job_id=None, pending=0)
    app_settings = getattr(request.app.state, "settings", None) or settings
    settle = float(getattr(app_settings, "inbox_settle_seconds", 60))
    folders = await run_in_threadpool(
        partial(settled_folders, inbox_dir, settle_seconds=settle, now=time.time())
    )
    if not folders:
        # Nothing to review right now — but distinguish WHY. An empty inbox is
        # "all done"; folders still receiving files are "not yet", and the caller
        # must not tell the user the inbox cleared while their rows are on screen.
        total = await run_in_threadpool(count_pending, inbox_dir)
        return ReviewInboxResponse(started=False, job_id=None, pending=0, in_flight=total)
    pending = len(folders)
    # Any listed item we did not hand over is still arriving; report it so the UI
    # can say so rather than implying the backlog is now empty.
    total = await run_in_threadpool(count_pending, inbox_dir)
    in_flight = max(0, total - pending)
    try:
        job_id = reg.start(
            [str(folder) for folder in folders],
            options=ImportOptions(operation="move"),
            origin="inbox",
        )
    except (LibraryRefusedError, LibraryRootUnavailableError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except RuntimeError:
        # An import is already running (single-slot policy) — TOCTOU after the gate.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An import is already running"
        ) from None
    return ReviewInboxResponse(started=True, job_id=job_id, pending=pending, in_flight=in_flight)


@router.get("/acquisition/inbox/items")
async def list_inbox_items(request: Request) -> InboxListing:
    """The inbox backlog — top-level folders awaiting review, source-agnostic.

    Read-only + never 500: a missing/empty inbox (or the lifespan-less test
    client, which has no ``inbox_dir``) yields an empty listing.
    """
    inbox_dir = getattr(request.app.state, "inbox_dir", None)
    if inbox_dir is None:
        return InboxListing(items=[])
    ledger: AcquisitionLedger | None = getattr(request.app.state, "acquisition_ledger", None)
    app_settings = getattr(request.app.state, "settings", None) or settings
    settle = float(getattr(app_settings, "inbox_settle_seconds", 60))
    # Same window "Review all" uses, so a row's in_flight cue agrees with whether
    # that button would actually import it.
    items = await run_in_threadpool(
        partial(list_inbox, inbox_dir, ledger, settle_seconds=settle, now=time.time())
    )
    return InboxListing(items=items)


@router.post(
    "/acquisition/inbox/items/import",
    responses={
        503: _LIBRARY_REFUSED_RESPONSE,
        404: {
            "model": ErrorDetail,
            "description": (
                "No inbox is configured, or that name does not resolve to a"
                " folder sitting directly inside the inbox."
            ),
        },
        # The shared refusal PLUS this route's own ambiguous-name guard, which
        # answers with the same status.
        409: {
            "model": ErrorDetail,
            "description": (
                "An import is already running, or a beets swap (such as a config Apply or"
                " duplicate resolve) or a lyrics backfill, an artist-art backfill, a"
                " reorganize backfill, or a disk sync holds the library, or"
                " two inbox folders display under the same name."
            ),
        },
    },
)
async def import_inbox_item(
    body: ImportInboxItemRequest,
    request: Request,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> ReviewInboxResponse:
    """Attended move-import of ONE inbox folder (the per-item Review action).

    Takes the folder ``name`` (not a path) and re-roots it under the inbox, so a
    client value cannot escape: ``contain(strict=True)`` rejects ``../``, absolute
    paths, the inbox root itself, symlink escapes, and malformed names (404). The
    shared import-slot gate refuses (409) while a mutation/backfill owns the slot.
    """
    ensure_import_can_start(request)
    inbox_dir: Path | None = getattr(request.app.state, "inbox_dir", None)
    if inbox_dir is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox item not found")
    # ``name`` is the display-safe value the listing emitted, so a folder whose
    # name is not valid UTF-8 comes back carrying placeholders; map it onto the
    # real entry before containing it, and refuse rather than guess when two
    # folders display alike.
    try:
        target = resolve_display_path(inbox_dir, body.name)
    except AmbiguousDisplayName:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Two inbox folders display under the same name because their names are "
                "not valid UTF-8. Rename one on disk to tell them apart."
            ),
        ) from None
    contained = contain(str(target), inbox_dir, strict=True)
    if contained is None or not is_dir(contained):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox item not found")
    try:
        job_id = reg.start(str(contained), options=ImportOptions(operation="move"), origin="inbox")
    except (LibraryRefusedError, LibraryRootUnavailableError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An import is already running"
        ) from None
    return ReviewInboxResponse(started=True, job_id=job_id, pending=1)
