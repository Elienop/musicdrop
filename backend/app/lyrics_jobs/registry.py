"""Single-slot, in-memory registry for the library-wide lyrics backfill.

Mirrors app/import_jobs/registry.py: at most one backfill runs at a time; a
second ``start`` raises RuntimeError (the API maps it to 409). Thread-safe — the
worker thread mutates counters/phase via these methods while API threads read
``state()``. In-memory only: a server restart loses the job (the sweep is
idempotent — skip-existing — so just re-run).

The common lifecycle lives in ``app.jobs.SingleSlotRegistry``; this module keeps
the backfill job fields, the ``record`` counters, and the wire ``state()``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app import library_busy
from app.jobs import JobState, SingleSlotRegistry
from app.models.lyrics import ItemLyricsOutcome, LyricsBackfillPhase, LyricsBackfillStatus


@dataclass
class _BackfillJob(JobState):
    phase: LyricsBackfillPhase = "running"
    found: int = 0
    not_found: int = 0
    failed: int = 0
    skipped: int = 0
    writes_enabled: bool = True
    album_id: int | None = None
    scope_label: str = "library"


class LyricsBackfillRegistry(SingleSlotRegistry[_BackfillJob]):
    """Holds the active (or last) backfill job."""

    job_type = library_busy.LYRICS

    def start(
        self, *, writes_enabled: bool, album_id: int | None = None, scope_label: str = "library"
    ) -> str:
        with self._claim("a lyrics backfill or another library operation is already running"):
            if self._job is not None and self._job.phase == "running":
                raise RuntimeError("a lyrics backfill is already running")
            job = _BackfillJob(
                id=uuid.uuid4().hex,
                writes_enabled=writes_enabled,
                album_id=album_id,
                scope_label=scope_label,
            )
            self._job = job
            return job.id

    def record(self, outcome: ItemLyricsOutcome) -> None:
        with self._lock:
            job = self._job
            if job is None:
                return
            job.processed += 1
            if outcome.status == "found":
                job.found += 1
            elif outcome.status == "not_found":
                job.not_found += 1
            elif outcome.status == "fetch_failed":
                job.failed += 1
            else:  # skipped_existing / skipped_no_metadata
                job.skipped += 1

    def state(self) -> LyricsBackfillStatus:
        with self._lock:
            job = self._job
            if job is None:
                return LyricsBackfillStatus(
                    phase="idle",
                    job_id=None,
                    total=0,
                    processed=0,
                    found=0,
                    not_found=0,
                    failed=0,
                    skipped=0,
                    current=None,
                    writes_enabled=False,
                    error=None,
                    album_id=None,
                    scope_label="library",
                )
            return LyricsBackfillStatus(
                phase=job.phase,
                job_id=job.id,
                total=job.total,
                processed=job.processed,
                found=job.found,
                not_found=job.not_found,
                failed=job.failed,
                skipped=job.skipped,
                current=job.current,
                writes_enabled=job.writes_enabled,
                error=job.error,
                album_id=job.album_id,
                scope_label=job.scope_label,
            )


# Process-global single slot (mirrors import_jobs.registry).
_registry = LyricsBackfillRegistry()


def get_lyrics_backfill() -> LyricsBackfillRegistry:
    """Return the live process-global backfill registry (usable as a FastAPI dep)."""
    return _registry


def lyrics_backfill_active() -> bool:
    """True while a backfill owns the slot — the shared mutual-exclusion check."""
    return _registry.is_running()


def reset_lyrics_backfill() -> LyricsBackfillRegistry:
    """Replace the global registry (test helper). Returns the new instance."""
    global _registry
    _registry = LyricsBackfillRegistry()
    return _registry
