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
