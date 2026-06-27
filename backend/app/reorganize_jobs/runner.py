"""The reorganize worker — a sequential library/artist/album sweep (the `beet move`
analog). ``sweep`` is the synchronous, directly-testable loop; ``start_backfill``
runs it on a daemon thread so the API start endpoint returns immediately. Pure
local file IO, so no courtesy delay is needed (default 0)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from app.beets.library import LibraryHandle, library_paths_context
from app.beets.reorganize import collect_units, reorganize_album, reorganize_singleton
from app.models.reorganize import ReorganizeOutcome, ReorganizeScope
from app.reorganize_jobs.registry import ReorganizeRegistry


def sweep(
    reg: ReorganizeRegistry,
    handle: LibraryHandle,
    *,
    scope: ReorganizeScope,
    artist: str | None = None,
    album_id: int | None = None,
    delay: float = 0.0,
    reorg_album: Callable[..., ReorganizeOutcome] = reorganize_album,
    reorg_singleton: Callable[..., ReorganizeOutcome] = reorganize_singleton,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Run the scoped sweep to completion. Never raises.

    ``on_complete`` fires once on termination (done/stopped/fail) — a partial
    run still moved files, so open tabs should refetch.
    """
    try:
        with library_paths_context(handle):
            albums, singletons = collect_units(
                handle.lib, scope=scope, artist=artist, album_id=album_id
            )
            reg.set_total(len(albums) + len(singletons))
            for album in albums:
                if reg.should_stop():
                    reg.finish("stopped")
                    return
                outcome = reorg_album(handle.lib, album)
                reg.record(outcome)
                reg.set_current(outcome.label)
                if delay:
                    time.sleep(delay)
            for item in singletons:
                if reg.should_stop():
                    reg.finish("stopped")
                    return
                outcome = reorg_singleton(handle.lib, item)
                reg.record(outcome)
                reg.set_current(outcome.label)
                if delay:
                    time.sleep(delay)
            reg.finish("done")
    except Exception as exc:  # any crash becomes a failed job, never a lost thread
        reg.fail(str(exc) or exc.__class__.__name__)
    finally:
        if on_complete is not None:
            on_complete()


def start_backfill(
    reg: ReorganizeRegistry,
    handle: LibraryHandle,
    *,
    scope: ReorganizeScope,
    artist: str | None = None,
    album_id: int | None = None,
    delay: float = 0.0,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Spawn the scoped sweep on a daemon thread (non-blocking)."""
    threading.Thread(
        target=lambda: sweep(
            reg,
            handle,
            scope=scope,
            artist=artist,
            album_id=album_id,
            delay=delay,
            on_complete=on_complete,
        ),
        name="musicdrop-reorganize",
        daemon=True,
    ).start()
