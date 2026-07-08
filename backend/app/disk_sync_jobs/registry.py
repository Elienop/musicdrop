"""Single-slot, in-memory registry for the disk-sync job (clone of
reorganize_jobs). At most one sync runs at a time; a second ``start`` raises
RuntimeError (the API maps it to 409). Thread-safe; in-memory only (a restart
loses the job; the sync is idempotent — just re-run).

The common lifecycle lives in ``app.jobs.SingleSlotRegistry``; this module keeps
the disk-sync job fields, the ``record`` counters, the emptied-album tally, and
the wire ``state()``. Note ``record`` sets ``current`` inline (rather than via
``set_current``), so the just-processed folder shows on the live status."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.jobs import JobState, SingleSlotRegistry
from app.models.disk_sync import (
    DiskSyncOutcome,
    DiskSyncPhase,
    DiskSyncReadError,
    DiskSyncStatus,
)

#: First N read errors surfaced on the wire (the counts stay exact).
FAILURE_ROW_CAP = 10


@dataclass
class _DiskSyncJob(JobState):
    phase: DiskSyncPhase = "running"
    removed: int = 0
    updated: int = 0
    unchanged: int = 0
    read_errors: int = 0
    emptied_albums: int = 0
    failures: list[DiskSyncReadError] = field(default_factory=list)


class DiskSyncRegistry(SingleSlotRegistry[_DiskSyncJob]):
    """Holds the active (or last) disk-sync job."""

    def start(self) -> str:
        with self._lock:
            if self._job is not None and self._job.phase == "running":
                raise RuntimeError("a disk sync is already running")
            job = _DiskSyncJob(id=uuid.uuid4().hex)
            self._job = job
            return job.id

    def record(self, outcome: DiskSyncOutcome) -> None:
        with self._lock:
            job = self._job
            if job is None:
                return
            job.processed += 1
            job.current = outcome.label
            if outcome.status == "removed":
                job.removed += 1
            elif outcome.status == "updated":
                job.updated += 1
            elif outcome.status == "read_error":
                job.read_errors += 1
                if len(job.failures) < FAILURE_ROW_CAP:
                    job.failures.append(
                        DiskSyncReadError(label=outcome.label, error=outcome.error or "")
                    )
            else:  # unchanged
                job.unchanged += 1

    def record_emptied(self, n: int) -> None:
        with self._lock:
            if self._job is not None:
                self._job.emptied_albums += n

    def state(self) -> DiskSyncStatus:
        with self._lock:
            job = self._job
            if job is None:
                return DiskSyncStatus(
                    phase="idle",
                    job_id=None,
                    total=0,
                    processed=0,
                    removed=0,
                    updated=0,
                    unchanged=0,
                    read_errors=0,
                    emptied_albums=0,
                    current=None,
                    error=None,
                    failures=[],
                )
            return DiskSyncStatus(
                phase=job.phase,
                job_id=job.id,
                total=job.total,
                processed=job.processed,
                removed=job.removed,
                updated=job.updated,
                unchanged=job.unchanged,
                read_errors=job.read_errors,
                emptied_albums=job.emptied_albums,
                current=job.current,
                error=job.error,
                failures=list(job.failures),
            )


# Process-global single slot (mirrors reorganize_jobs).
_registry = DiskSyncRegistry()


def get_disk_sync_registry() -> DiskSyncRegistry:
    """Return the live process-global disk-sync registry (FastAPI dep)."""
    return _registry


def disk_sync_active() -> bool:
    """True while a disk sync owns the slot — the shared mutual-exclusion check."""
    return _registry.is_running()


def reset_disk_sync() -> DiskSyncRegistry:
    """Replace the global registry (test helper). Returns the new instance."""
    global _registry
    _registry = DiskSyncRegistry()
    return _registry
