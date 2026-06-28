"""Library-wide lyrics endpoints: coverage + the backfill job.

Per-album fetch lives on the albums router (POST /albums/{id}/lyrics/fetch).
The backfill is a single-slot background job (app/lyrics_jobs), mutually
exclusive with imports and library writes.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from app.beets.lyrics import lyrics_coverage, writes_enabled
from app.events.emit import emit_library_changed
from app.import_jobs.registry import get_registry
from app.lyrics_jobs.registry import (
    LyricsBackfillRegistry,
    get_lyrics_backfill,
)
from app.lyrics_jobs.runner import start_backfill
from app.models.lyrics import LyricsBackfillStatus, LyricsCoverage

router = APIRouter(tags=["lyrics"])


@router.get("/lyrics/coverage", response_model=LyricsCoverage)
async def get_lyrics_coverage(request: Request) -> LyricsCoverage:
    """Fraction of library tracks that already carry lyrics. Read-only."""
    handle = request.app.state.beets_library
    return await run_in_threadpool(lyrics_coverage, handle.lib)


@router.post("/lyrics/backfill", response_model=LyricsBackfillStatus)
async def start_lyrics_backfill(
    request: Request,
    reg: Annotated[LyricsBackfillRegistry, Depends(get_lyrics_backfill)],
    recheck_misses: bool = False,
) -> LyricsBackfillStatus:
    """Start a library-wide backfill. 409 if an import, another backfill, or a
    config-apply/edit/cover op is in flight."""
    from app.artist_art_jobs.registry import artist_art_backfill_active

    app = request.app
    if get_registry().has_active_job():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An import is in progress — backfill available when it finishes",
        )
    if artist_art_backfill_active():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An artist-art job is in progress — backfill available when it finishes",
        )
    from app.reorganize_jobs.registry import reorganize_backfill_active

    if reorganize_backfill_active():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A reorganize is in progress — backfill available when it finishes",
        )
    lock = getattr(app.state, "beets_swap_lock", None)
    if lock is not None and lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A library operation is in progress — backfill available when it finishes",
        )
    write = writes_enabled()
    try:
        reg.start(writes_enabled=write)
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A lyrics backfill is already running"
        ) from None
    handle = app.state.beets_library
    # app.state.settings is set by the lifespan; the TestClient skips it, so fall
    # back to a default (the plan's intent — getattr default covers the absence).
    app_settings = getattr(app.state, "settings", None)
    delay = float(getattr(app_settings, "lyrics_backfill_delay_seconds", 0.2))
    start_backfill(
        reg,
        handle,
        delay=delay,
        write=write,
        recheck_misses=recheck_misses,
        on_complete=lambda: emit_library_changed(app),
    )
    return reg.state()


@router.get("/lyrics/backfill", response_model=LyricsBackfillStatus)
async def get_lyrics_backfill_status(
    reg: Annotated[LyricsBackfillRegistry, Depends(get_lyrics_backfill)],
) -> LyricsBackfillStatus:
    """Poll the backfill (phase + counters). Returns phase=idle when none ran."""
    return reg.state()


@router.post("/lyrics/backfill/stop", response_model=LyricsBackfillStatus)
async def stop_lyrics_backfill(
    reg: Annotated[LyricsBackfillRegistry, Depends(get_lyrics_backfill)],
) -> LyricsBackfillStatus:
    """Request a cooperative stop; the worker ends after the current track."""
    reg.request_stop()
    return reg.state()
