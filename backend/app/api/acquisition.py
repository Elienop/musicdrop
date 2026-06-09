"""The acquisition status probe + the one-click inbox review.

``GET /acquisition/status`` is a read-only probe over the lifespan-constructed
``AcquisitionQueue`` (informational only — the queue is not a mutex participant,
Option A). Under the lifespan-less test client there is no queue on
``app.state``, so it falls back to an idle status rather than 500.

``POST /acquisition/review-inbox`` is the slskd-panel one-click review: it
resolves the fixed inbox path SERVER-SIDE (never sent to the browser) and starts
a normal *attended* import with ``operation="move"`` so applied albums leave the
inbox. An empty inbox is a no-op (``started=False``), never an error.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from app.api.import_ import ensure_import_can_start
from app.import_jobs.registry import ImportJobRegistry, get_registry
from app.models.acquisition import AcquisitionQueueStatus, ReviewInboxResponse
from app.models.import_models import ImportOptions

router = APIRouter(tags=["acquisition"])

# The ledger file the queue persists under the inbox — never an album folder, so
# it does not count toward "is there anything to review".
_LEDGER_FILENAME = ".musicdrop-ledger.json"


@router.get("/acquisition/status", response_model=AcquisitionQueueStatus)
async def get_acquisition_status(request: Request) -> AcquisitionQueueStatus:
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
        )
    snapshot: AcquisitionQueueStatus = queue.status()
    return snapshot


def _count_pending(inbox_dir: Path) -> int:
    """Non-hidden immediate children of the inbox, excluding the ledger file.

    A cheap proxy for "is there anything to review": after an auto-import run the
    inbox holds exactly the set-aside albums (strong matches were already moved
    out). Hidden dotfiles and the ledger file never count.
    """
    try:
        return sum(
            1
            for entry in os.scandir(inbox_dir)
            if not entry.name.startswith(".") and entry.name != _LEDGER_FILENAME
        )
    except OSError:
        return 0


@router.post("/acquisition/review-inbox", response_model=ReviewInboxResponse)
async def review_inbox(
    request: Request,
    reg: Annotated[ImportJobRegistry, Depends(get_registry)],
) -> ReviewInboxResponse:
    """Start an attended, move-mode import of the fixed slskd inbox dir.

    One-click review of the set-aside backlog from the slskd panel: no path is
    typed and the absolute inbox path never leaves the server. Strong matches
    auto-apply (and move out of the inbox); uncertain ones park for review in the
    normal candidate-review screen. An empty inbox is a no-op (``started=False``),
    never an error — and the shared import-slot gate refuses (409) while another
    beets mutation or backfill owns the slot.
    """
    ensure_import_can_start(request)
    inbox_dir: Path | None = getattr(request.app.state, "inbox_dir", None)
    if inbox_dir is None:
        return ReviewInboxResponse(started=False, job_id=None, pending=0)
    pending = await run_in_threadpool(_count_pending, inbox_dir)
    if pending == 0:
        return ReviewInboxResponse(started=False, job_id=None, pending=0)
    try:
        job_id = reg.start(
            str(inbox_dir),
            options=ImportOptions(operation="move"),
            origin="inbox",
        )
    except RuntimeError:
        # An import is already running (single-slot policy) — TOCTOU after the gate.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An import is already running"
        ) from None
    return ReviewInboxResponse(started=True, job_id=job_id, pending=pending)
