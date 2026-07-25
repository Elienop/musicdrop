"""Single-slot, in-memory registry for the artist-art backfill (mirrors lyrics_jobs).

The common lifecycle lives in ``app.jobs.SingleSlotRegistry``; this module keeps
the backfill job fields, the ``record`` counters, and the wire ``state()``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app import library_busy
from app.jobs import JobState, SingleSlotRegistry
from app.models.artist_art import ArtistArtBackfillPhase, ArtistArtBackfillStatus, ArtistArtOutcome


@dataclass
class _BackfillJob(JobState):
    phase: ArtistArtBackfillPhase = "running"
    written: int = 0
    skipped: int = 0
    failed: int = 0
    artist: str | None = None
    scope_label: str = "library"
    force: bool = False


class ArtistArtBackfillRegistry(SingleSlotRegistry[_BackfillJob]):
    job_type = library_busy.ARTIST_ART

    def start(self, *, force: bool, artist: str | None = None, scope_label: str = "library") -> str:
        with self._claim("an artist-art backfill or another library operation is already running"):
            if self._job is not None and self._job.phase == "running":
                raise RuntimeError("an artist-art backfill is already running")
            job = _BackfillJob(
                id=uuid.uuid4().hex, artist=artist, scope_label=scope_label, force=force
            )
            self._job = job
            return job.id

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
