"""Single-slot, in-memory registry for the library-wide lyrics backfill.

Mirrors app/import_jobs/registry.py: at most one backfill runs at a time; a
second ``start`` raises RuntimeError (the API maps it to 409). Thread-safe — the
worker thread mutates counters/phase via these methods while API threads read
``state()``. In-memory only: a server restart loses the job (the sweep is
idempotent — skip-existing — so just re-run).
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from app.models.lyrics import ItemLyricsOutcome, LyricsBackfillPhase, LyricsBackfillStatus


@dataclass
class _BackfillJob:
    id: str
    phase: LyricsBackfillPhase = "running"
    total: int = 0
    processed: int = 0
    found: int = 0
    not_found: int = 0
    failed: int = 0
    skipped: int = 0
    current: str | None = None
    writes_enabled: bool = True
    error: str | None = None
    album_id: int | None = None
    scope_label: str = "library"
    stop_requested: bool = False


class LyricsBackfillRegistry:
    """Holds the active (or last) backfill job."""

    def __init__(self) -> None:
        self._job: _BackfillJob | None = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.phase == "running"

    def start(
        self, *, writes_enabled: bool, album_id: int | None = None, scope_label: str = "library"
    ) -> str:
        with self._lock:
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

    def set_total(self, total: int) -> None:
        with self._lock:
            if self._job is not None:
                self._job.total = total

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

    def set_current(self, label: str | None) -> None:
        with self._lock:
            if self._job is not None:
                self._job.current = label

    def should_stop(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.stop_requested

    def request_stop(self) -> None:
        with self._lock:
            if self._job is not None and self._job.phase == "running":
                self._job.stop_requested = True

    def finish(self, phase: LyricsBackfillPhase) -> None:
        with self._lock:
            if self._job is not None and self._job.phase == "running":
                self._job.phase = phase
                self._job.current = None

    def fail(self, message: str) -> None:
        with self._lock:
            if self._job is not None:
                self._job.phase = "failed"
                self._job.error = message
                self._job.current = None

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
