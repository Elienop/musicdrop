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

from app.acquisition.inbox import contain
from app.acquisition.ledger import AcquisitionLedger
from app.api.import_ import ensure_import_can_start
from app.import_jobs.registry import ImportJobRegistry, get_registry
from app.models.acquisition import (
    AcquisitionQueueStatus,
    ImportInboxItemRequest,
    InboxItem,
    InboxListing,
    LedgerOutcome,
    ReviewInboxResponse,
)
from app.models.import_models import ImportOptions

router = APIRouter(tags=["acquisition"])

# The ledger file the queue persists under the inbox — never an album folder, so
# it does not count toward "is there anything to review".
_LEDGER_FILENAME = ".musicdrop-ledger.json"

# Extensions we treat as audio when deciding whether an inbox folder holds music.
_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma", ".aiff"}


@router.get("/acquisition/status", response_model=AcquisitionQueueStatus)
async def get_acquisition_status(request: Request) -> AcquisitionQueueStatus:
    inbox_dir = getattr(request.app.state, "inbox_dir", None)
    inbox_pending = (
        await run_in_threadpool(_count_pending, inbox_dir) if inbox_dir is not None else 0
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


def _count_pending(inbox_dir: Path) -> int:
    """Count top-level inbox folders holding audio — the SAME item definition the
    Review listing uses, so the nav badge can't show a phantom count from a loose
    non-audio file, an empty leftover dir, or a symlink. Skips hidden entries, the
    ledger file, and symlinked entries (parity with the import path's symlink
    guard). 0 on any OS error.
    """
    try:
        entries = list(os.scandir(inbox_dir))
    except OSError:
        return 0
    count = 0
    for entry in entries:
        if entry.name.startswith(".") or entry.name == _LEDGER_FILENAME:
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        if _has_audio(Path(entry.path)):
            count += 1
    return count


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


def _has_audio(folder: Path) -> bool:
    """True as soon as one audio file is found beneath ``folder`` (early-exit).

    ``os.walk``'s ``followlinks`` default is False, so a symlinked subdir (or a
    symlink loop) inside a real inbox folder is never descended into.
    """
    try:
        for _root, _dirs, files in os.walk(folder):
            if any(os.path.splitext(f)[1].lower() in _AUDIO_EXTS for f in files):
                return True
    except OSError:
        return False
    return False


def _audio_stats(folder: Path) -> tuple[int, int]:
    """``(track_count, total_bytes)`` of audio files beneath ``folder``."""
    count = 0
    size = 0
    try:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if os.path.splitext(name)[1].lower() not in _AUDIO_EXTS:
                    continue
                count += 1
                try:
                    size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        return count, size
    return count, size


def _list_inbox(inbox_dir: Path, ledger: AcquisitionLedger | None) -> list[InboxItem]:
    """Top-level non-hidden inbox dirs holding audio, ledger-annotated (never filtered).

    An item = one immediate child directory with >=1 audio file beneath it (empty
    leftovers after a successful move-out are skipped). A set-aside item IS in the
    ledger, so the ledger only ANNOTATES (``set_aside``/``failed``) — it never
    removes a row. The ledger keys the (possibly deeper) album path the webhook
    coalesced, so a row is annotated when a ledger entry sits at or under it.
    """
    items: list[InboxItem] = []
    try:
        entries = list(os.scandir(inbox_dir))
    except OSError:
        return items
    ledger_rows = ledger.entries() if ledger is not None else []
    for entry in entries:
        if entry.name.startswith(".") or entry.name == _LEDGER_FILENAME:
            continue
        # Skip symlinked entries: the import path's contain() rejects symlink
        # escapes, so the listing must not follow one out of the inbox either.
        if not entry.is_dir(follow_symlinks=False):
            continue
        tracks, size = _audio_stats(Path(entry.path))
        if tracks == 0:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        folder = Path(entry.path).resolve()
        outcome: LedgerOutcome | None = None
        for row in ledger_rows:
            if row.outcome not in ("set_aside", "failed"):
                continue
            try:
                row_path = Path(row.path).resolve()
            except (OSError, ValueError):
                continue  # a corrupt/NUL on-disk ledger path never 500s the list
            if row_path == folder or folder in row_path.parents:
                outcome = row.outcome
                break
        items.append(
            InboxItem(
                name=entry.name,
                mtime=st.st_mtime,
                size=size,
                track_count=tracks,
                outcome=outcome,
            )
        )
    items.sort(key=lambda i: i.mtime, reverse=True)
    return items


@router.get("/acquisition/inbox/items", response_model=InboxListing)
async def list_inbox_items(request: Request) -> InboxListing:
    """The inbox backlog — top-level folders awaiting review, source-agnostic.

    Read-only + never 500: a missing/empty inbox (or the lifespan-less test
    client, which has no ``inbox_dir``) yields an empty listing.
    """
    inbox_dir = getattr(request.app.state, "inbox_dir", None)
    if inbox_dir is None:
        return InboxListing(items=[])
    ledger: AcquisitionLedger | None = getattr(request.app.state, "acquisition_ledger", None)
    items = await run_in_threadpool(_list_inbox, inbox_dir, ledger)
    return InboxListing(items=items)


@router.post("/acquisition/inbox/items/import", response_model=ReviewInboxResponse)
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
    contained = contain(str(inbox_dir / body.name), inbox_dir, strict=True)
    if contained is None or not contained.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inbox item not found")
    try:
        job_id = reg.start(str(contained), options=ImportOptions(operation="move"), origin="inbox")
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An import is already running"
        ) from None
    return ReviewInboxResponse(started=True, job_id=job_id, pending=1)
