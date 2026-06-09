"""Pydantic contract for the acquisition seam (ledger + queue status).

No beets imports — these are plain data shapes the API and the ledger persist.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# How an inbox folder ended up after the unattended import drained it.
LedgerOutcome = Literal["imported", "set_aside", "failed"]


class LedgerEntry(BaseModel):
    """One handled inbox folder, keyed by path + identity (mtime/size).

    ``mtime`` is the folder's ``st_mtime`` as a float (NEVER ``st_mtime_ns`` — a
    nanosecond int near 1e18 loses precision through JSON's number type). A
    folder whose mtime or size changed after marking no longer matches, so a
    re-download under the same name re-imports.
    """

    path: str
    mtime: float
    size: int
    outcome: LedgerOutcome


class AcquisitionQueueStatus(BaseModel):
    """Informational snapshot of the serial acquisition queue.

    ``phase`` is ``"running"`` while the drain holds the import slot (or is
    waiting for the gate to clear), else ``"idle"``. The counters are
    process-lifetime totals; ``error`` carries the last drain error (best-effort,
    never raised to the caller). Status is informational only — the queue is NOT
    a mutex participant (it consumes the existing import-slot gate).
    """

    phase: Literal["idle", "running"]
    queued: int
    current: str | None
    processed: int
    set_aside: int
    failed: int
    error: str | None
    # How many folders are sitting in the inbox right now (a cheap scandir count,
    # NOT the lifetime set_aside total) — feeds the nav Review badge.
    inbox_pending: int = 0


class InboxItem(BaseModel):
    """One top-level inbox folder awaiting review (a backlog row).

    ``name`` is the immediate inbox child dir (also the import target id).
    ``outcome`` is best-effort: ``set_aside``/``failed`` iff a ledger entry at or
    under this folder has that outcome, else ``None`` (a fresh drop). ``mtime`` is
    a float (NEVER ``st_mtime_ns`` — a nanosecond int loses JSON precision).
    """

    name: str
    mtime: float
    size: int
    track_count: int
    outcome: LedgerOutcome | None = None
    source: str = "slskd"


class InboxListing(BaseModel):
    """The inbox backlog (``GET /api/acquisition/inbox/items``)."""

    items: list[InboxItem] = []


class ImportInboxItemRequest(BaseModel):
    """Body of ``POST /api/acquisition/inbox/items/import`` — one folder by name."""

    name: str


class ReviewInboxResponse(BaseModel):
    """Result of ``POST /api/acquisition/review-inbox`` (the slskd-panel review).

    ``started`` is True iff an attended import of the inbox was kicked off, with
    ``job_id`` the running job to navigate to. An empty inbox is a no-op
    (``started=False, job_id=None``), never an error.
    """

    started: bool
    job_id: str | None = None
    pending: int = 0
