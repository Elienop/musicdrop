"""Single source of truth for the "is a library-mutating job running?" gate.

Five mutually-exclusive job types can hold the library: an import, and the
lyrics / artist-art / reorganize / disk-sync backfills. Many endpoints refuse
(409) while any of them runs; the api-layer sites ALSO refuse while the beets
swap lock is held (a config Apply or duplicate resolve mid-flight). This module
is the ONE place that union lives, so a sixth job type is wired in exactly once.

The job predicates are imported lazily inside the functions so the live binding
is read at call time: tests monkeypatch the source-module attributes, and the
import-job registry is a swappable global (``get_registry()`` reads it fresh).
Kept out of ``app/api`` and ``app/beets`` so the beets adapter and the
acquisition producers can both consume it without crossing either boundary
(``app/import_jobs/gates.py`` must not import the api layer; the beets adapter
must not gain a web dependency at module load).
"""

from __future__ import annotations

from collections.abc import Container

# Names the five single-slot job types accept in ``exclude`` to drop their own
# slot from the union (a job's own start-gate must not see itself as busy).
_IMPORT = "import"
_LYRICS = "lyrics"
_ARTIST_ART = "artist_art"
_REORGANIZE = "reorganize"
_DISK_SYNC = "disk_sync"


def library_job_active(*, exclude: Container[str] = ()) -> bool:
    """True while any library-mutating background job is active.

    The union of the five single-slot job types, keyed by name so a caller's own
    start-gate can drop itself via ``exclude``: ``"import"`` (the import slot),
    ``"lyrics"``, ``"artist_art"``, ``"reorganize"``, ``"disk_sync"``.
    Best-effort and read live at call time.
    """
    from app.artist_art_jobs.registry import artist_art_backfill_active
    from app.disk_sync_jobs.registry import disk_sync_active
    from app.import_jobs.registry import get_registry
    from app.lyrics_jobs.registry import lyrics_backfill_active
    from app.reorganize_jobs.registry import reorganize_backfill_active

    return (
        (_IMPORT not in exclude and get_registry().has_active_job())
        or (_LYRICS not in exclude and lyrics_backfill_active())
        or (_ARTIST_ART not in exclude and artist_art_backfill_active())
        or (_REORGANIZE not in exclude and reorganize_backfill_active())
        or (_DISK_SYNC not in exclude and disk_sync_active())
    )


def raise_if_library_busy(
    app: object,
    *,
    exclude: Container[str] = (),
    message: str = "A library operation is in progress — try again when it finishes",
) -> None:
    """Raise ``HTTPException(409, message)`` if a library job is active OR the
    beets swap lock is held — the api-layer variant of the gate.

    ``app`` is the FastAPI app (duck-typed ``object`` so tests can pass a stub
    carrying ``state``); ``exclude`` drops the caller's own job from the union;
    ``message`` is the 409 detail. Best-effort ``Lock.locked()`` — the same
    single-user TOCTOU posture the individual sites always used.
    """
    from fastapi import HTTPException, status

    if library_job_active(exclude=exclude):
        raise HTTPException(status.HTTP_409_CONFLICT, message)
    lock = getattr(getattr(app, "state", None), "beets_swap_lock", None)
    if lock is not None and lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, message)
