"""Single-slot, in-memory registry for the artist-art backfill (mirrors lyrics_jobs)."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from app.models.artist_art import ArtistArtBackfillPhase, ArtistArtBackfillStatus, ArtistArtOutcome


@dataclass
class _BackfillJob:
    id: str
    phase: ArtistArtBackfillPhase = "running"
    total: int = 0
    processed: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    current: str | None = None
    error: str | None = None
    artist: str | None = None
    scope_label: str = "library"
    force: bool = False
    stop_requested: bool = False


class ArtistArtBackfillRegistry:
    def __init__(self) -> None:
        self._job: _BackfillJob | None = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        with self._lock:
            return self._job is not None and self._job.phase == "running"

    def start(self, *, force: bool, artist: str | None = None, scope_label: str = "library") -> str:
        with self._lock:
            if self._job is not None and self._job.phase == "running":
                raise RuntimeError("an artist-art backfill is already running")
            job = _BackfillJob(
                id=uuid.uuid4().hex, artist=artist, scope_label=scope_label, force=force
            )
            self._job = job
            return job.id

    def set_total(self, total: int) -> None:
        with self._lock:
            if self._job is not None:
                self._job.total = total

    def record(self, outcome: ArtistArtOutcome) -> None:
        with self._lock:
            job = self._job
            if job is None:
                return
            job.processed += 1
            if outcome.status == "written":
                job.written += 1
            elif outcome.status == "failed":
                job.failed += 1
            else:  # skipped / no_art / no_folder
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

    def finish(self, phase: ArtistArtBackfillPhase) -> None:
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

    def state(self) -> ArtistArtBackfillStatus:
        with self._lock:
            job = self._job
            if job is None:
                return ArtistArtBackfillStatus(
                    phase="idle",
                    job_id=None,
                    total=0,
                    processed=0,
                    written=0,
                    skipped=0,
                    failed=0,
                    current=None,
                    error=None,
                    artist=None,
                    scope_label="library",
                )
            return ArtistArtBackfillStatus(
                phase=job.phase,
                job_id=job.id,
                total=job.total,
                processed=job.processed,
                written=job.written,
                skipped=job.skipped,
                failed=job.failed,
                current=job.current,
                error=job.error,
                artist=job.artist,
                scope_label=job.scope_label,
            )


_registry = ArtistArtBackfillRegistry()


def get_artist_art_backfill() -> ArtistArtBackfillRegistry:
    return _registry


def artist_art_backfill_active() -> bool:
    return _registry.is_running()


def reset_artist_art_backfill() -> ArtistArtBackfillRegistry:
    global _registry
    _registry = ArtistArtBackfillRegistry()
    return _registry
