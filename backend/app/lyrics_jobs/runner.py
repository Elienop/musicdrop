"""The lyrics-backfill worker — a sequential library sweep (the `beet lyrics` analog).

``sweep`` is the synchronous, directly-testable loop (snapshot items, fetch each
with skip-existing, record progress, courtesy-sleep, honor Stop). ``start_backfill``
runs it on a daemon thread so the API start endpoint returns immediately. beets
itself does no throttling; the courtesy ``delay`` keeps us polite to LRCLib.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from app.beets.library import LibraryHandle, library_paths_context
from app.beets.lyrics import collect_lyrics_units, fetch_item_lyrics, make_lyrics_plugin
from app.lyrics_jobs.registry import LyricsBackfillRegistry
from app.models.lyrics import ItemLyricsOutcome


def sweep(
    reg: LyricsBackfillRegistry,
    handle: LibraryHandle,
    *,
    delay: float,
    write: bool,
    album_id: int | None = None,
    fetch_one: Callable[..., ItemLyricsOutcome] = fetch_item_lyrics,
    make_plugin: Callable[[], Any] = make_lyrics_plugin,  # beets plugin stays opaque
) -> None:
    """Run the (library- or album-scoped) sweep to completion. Never raises."""
    try:
        # Bound for the WHOLE loop: the per-item file writes (try_write) must
        # resolve real paths on this worker thread, not just the snapshot.
        with library_paths_context(handle):
            plugin = make_plugin()
            units = collect_lyrics_units(handle, album_id)
            reg.set_total(len(units))
            for unit in units:
                if reg.should_stop():
                    reg.finish("stopped")
                    return
                outcome = fetch_one(plugin, unit.item, force=False, write=write)
                reg.record(outcome)
                reg.set_current(unit.label)
                if delay:
                    time.sleep(delay)
            reg.finish("done")
    except Exception as exc:  # any crash becomes a failed job, never a lost thread
        reg.fail(str(exc) or exc.__class__.__name__)


def start_backfill(
    reg: LyricsBackfillRegistry,
    handle: LibraryHandle,
    *,
    delay: float,
    write: bool,
    album_id: int | None = None,
) -> None:
    """Spawn the (library- or album-scoped) sweep on a daemon thread (non-blocking)."""
    threading.Thread(
        target=lambda: sweep(reg, handle, delay=delay, write=write, album_id=album_id),
        name="musicdrop-lyrics-backfill",
        daemon=True,
    ).start()
