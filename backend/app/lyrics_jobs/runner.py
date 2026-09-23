"""The lyrics-backfill worker — a sequential library sweep (the `beet lyrics` analog).

``sweep`` is the synchronous, directly-testable loop (snapshot items, fetch each
with skip-existing/skip-checked, record progress, courtesy-sleep, honor Stop).
``start_backfill`` runs it on a daemon thread so the API start endpoint returns
immediately. beets throttles/retries the HTTP itself; the courtesy ``delay``
is just an inter-track pause.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

from app.beets.library import LibraryHandle, library_paths_context
from app.beets.lyrics import (
    active_source_names,
    collect_lyrics_units,
    fetch_item_lyrics,
    make_lyrics_plugin,
)
from app.lyrics_jobs.registry import LyricsBackfillRegistry
from app.models.lyrics import ItemLyricsOutcome

_log = logging.getLogger(__name__)


def sweep(
    reg: LyricsBackfillRegistry,
    handle: LibraryHandle,
    *,
    delay: float,
    write: bool,
    album_id: int | None = None,
    recheck_misses: bool = False,
    fetch_one: Callable[..., ItemLyricsOutcome] = fetch_item_lyrics,
    make_plugin: Callable[..., Any] = make_lyrics_plugin,  # beets plugin stays opaque
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Run the (library- or album-scoped) sweep to completion. Never raises.

    ``on_complete`` fires once on termination (done/stopped/fail) — a partial
    run still changed lyrics, so open tabs should refetch.
    """
    try:
        # Bound for the WHOLE loop: the per-item file writes (try_write) must
        # resolve real paths on this worker thread, not just the snapshot.
        with library_paths_context(handle):
            # Library sweeps (album_id is None) query LRCLib only — Genius 429s a
            # big bulk run; it stays available for the targeted per-album fetch.
            plugin = make_plugin(lrclib_only=album_id is None)
            units = collect_lyrics_units(handle, album_id)
            reg.set_total(len(units))
            scope = "album" if album_id is not None else "library"
            _log.info(
                "lyrics %s sweep: %d tracks · sources=%s · writes=%s · recheck_misses=%s",
                scope,
                len(units),
                active_source_names(plugin),
                write,
                recheck_misses,
            )
            tally: Counter[str] = Counter()
            for unit in units:
                if reg.should_stop():
                    reg.finish("stopped")
                    _log.info("lyrics %s sweep stopped: %s", scope, dict(tally))
                    return
                outcome = fetch_one(
                    plugin, unit.item, force=False, write=write, recheck_misses=recheck_misses
                )
                reg.record(outcome)
                reg.set_current(unit.label)
                tally[outcome.status] += 1
                _log.debug(
                    "lyrics %s: %s%s",
                    unit.label,
                    outcome.status,
                    f" via {outcome.source}" if outcome.source else "",
                )
                if delay:
                    time.sleep(delay)
            reg.finish("done")
            _log.info("lyrics %s sweep done: %s", scope, dict(tally))
    except Exception as exc:  # any crash becomes a failed job, never a lost thread
        reg.fail(str(exc) or exc.__class__.__name__)
    finally:
        if on_complete is not None:
            on_complete()


def start_backfill(
    reg: LyricsBackfillRegistry,
    handle: LibraryHandle,
    *,
    delay: float,
    write: bool,
    album_id: int | None = None,
    recheck_misses: bool = False,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Spawn the (library- or album-scoped) sweep on a daemon thread.

    Via ``reg.spawn_worker`` so a refused ``Thread.start()`` frees the slot
    instead of wedging every library mutation (see SingleSlotRegistry)."""
    reg.spawn_worker(
        lambda: sweep(
            reg,
            handle,
            delay=delay,
            write=write,
            album_id=album_id,
            recheck_misses=recheck_misses,
            on_complete=on_complete,
        ),
        name="musicdrop-lyrics-backfill",
    )
