"""Single-slot, in-memory registry for the reorganize job (clone of lyrics_jobs).

At most one reorganize runs at a time; a second ``start`` raises RuntimeError
(the API maps it to 409). Thread-safe — the worker thread mutates counters/phase
while API threads read ``state()``. In-memory only (a restart loses the job; the
sweep is idempotent — skip-already-organized — so just re-run).

The common lifecycle (start/stop/finish/fail/counters) lives in
``app.jobs.SingleSlotRegistry``; this module keeps only the reorganize-specific
job fields, ``record`` counters, the orphan tally, and the wire ``state()``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app import library_busy
from app.jobs import JobState, SingleSlotRegistry
from app.models.reorganize import (
    ReorganizeBackfillStatus,
    ReorganizeOutcome,
    ReorganizePhase,
    ReorganizeScope,
    ReorganizeUnitFailure,
)

#: Cap on the per-unit failure rows carried on the wire status (count stays exact).
FAILURE_ROW_CAP = 10


@dataclass
class _ReorganizeJob(JobState):
    phase: ReorganizePhase = "running"
    scope: ReorganizeScope = "library"
    moved: int = 0
    skipped: int = 0
    failed: int = 0
    artist: str | None = None
    album_id: int | None = None
    scope_label: str = "library"
    orphans_trashed: int = 0
    failures: list[ReorganizeUnitFailure] = field(default_factory=list)


class ReorganizeRegistry(SingleSlotRegistry[_ReorganizeJob]):
    """Holds the active (or last) reorganize job."""

    job_type = library_busy.REORGANIZE

    def start(
        self,
        *,
        scope: ReorganizeScope,
        artist: str | None,
        album_id: int | None,
        scope_label: str,
    ) -> str:
        with self._claim("a reorganize or another library operation is already running"):
            if self._job is not None and self._job.phase == "running":
                raise RuntimeError("a reorganize is already running")
            job = _ReorganizeJob(
                id=uuid.uuid4().hex,
                scope=scope,
                artist=artist,
                album_id=album_id,
                scope_label=scope_label,
            )
            self._job = job
            return job.id

    def record(self, outcome: ReorganizeOutcome) -> None:
        with self._lock:
            job = self._job
            if job is None:
                return
            job.processed += 1
            if outcome.status == "moved":
                job.moved += 1
            elif outcome.status == "failed":
                job.failed += 1
                if len(job.failures) < FAILURE_ROW_CAP:
                    job.failures.append(
                        ReorganizeUnitFailure(label=outcome.label, error=outcome.error or "")
                    )
            else:  # skipped
                job.skipped += 1

    def record_orphans(self, n: int) -> None:
        with self._lock:
            if self._job is not None:
                self._job.orphans_trashed += n

    def state(self) -> ReorganizeBackfillStatus:
        with self._lock:
            job = self._job
            if job is None:
                return ReorganizeBackfillStatus(
                    phase="idle",
                    job_id=None,
                    scope=None,
                    total=0,
                    processed=0,
                    moved=0,
                    skipped=0,
                    failed=0,
                    current=None,
                    error=None,
                    artist=None,
                    album_id=None,
                    scope_label="library",
                    orphans_trashed=0,
                    failures=[],
                )
            return ReorganizeBackfillStatus(
                phase=job.phase,
                job_id=job.id,
                scope=job.scope,
                total=job.total,
                processed=job.processed,
                moved=job.moved,
                skipped=job.skipped,
                failed=job.failed,
                current=job.current,
                error=job.error,
                artist=job.artist,
                album_id=job.album_id,
                scope_label=job.scope_label,
                orphans_trashed=job.orphans_trashed,
                failures=list(job.failures),
            )


# Process-global single slot (mirrors lyrics_jobs / artist_art_jobs).
_registry = ReorganizeRegistry()


def get_reorganize_backfill() -> ReorganizeRegistry:
    """Return the live process-global reorganize registry (FastAPI dep)."""
    return _registry


def reorganize_backfill_active() -> bool:
    """True while a reorganize owns the slot — the shared mutual-exclusion check."""
    return _registry.is_running()


def reset_reorganize_backfill() -> ReorganizeRegistry:
    """Replace the global registry (test helper). Returns the new instance."""
    global _registry
    _registry = ReorganizeRegistry()
    return _registry
