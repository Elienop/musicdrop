"""The disk-sync worker: one library-wide walk (the ``beet update`` analog).
``sweep`` is the synchronous, directly-testable body; ``start_backfill`` runs
it on a daemon thread so the API start endpoint returns immediately."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.beets.disk_sync import run_disk_sync
from app.beets.library import (
    LibraryHandle,
    LibraryRootUnavailableError,
    library_paths_context,
)
from app.disk_sync_jobs.registry import DiskSyncRegistry
from app.models.disk_sync import DiskSyncOutcome
from app.playlists.reexport import reexport_playlists_containing_sync

_log = logging.getLogger(__name__)


def sweep(
    reg: DiskSyncRegistry,
    handle: LibraryHandle,
    *,
    playlists_dir: Path | None = None,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Run the sync to completion. Never raises.

    ``on_complete`` fires once on termination (done/stopped/failed) — a partial
    run still changed the DB, so open tabs should refetch.

    When ``playlists_dir`` is given, a `.m3u8` re-export pass runs after the walk
    for every playlist holding a track this sync REMOVED: the file is gone from
    disk, so the export must stop naming it. Omit it (the default) and the pass is
    skipped, leaving existing callers unchanged.

    The pass runs ONLY on the normal return. That is deliberate for the
    ``LibraryRootUnavailableError`` exit in particular: with the share unmounted
    every track looks missing, so re-exporting there would rewrite each `.m3u8`
    down to nothing — turning a mount blip into real data loss. A failed sync
    leaves the exports alone; the next successful run repairs them.
    """
    removed_ids: set[int] = set()

    def record(outcome: DiskSyncOutcome) -> None:
        if outcome.status == "removed" and outcome.item_id is not None:
            removed_ids.add(outcome.item_id)
        reg.record(outcome)

    try:
        with library_paths_context(handle):
            emptied = run_disk_sync(
                handle.lib,
                on_total=reg.set_total,
                on_item=record,
                should_stop=reg.should_stop,
            )
            reg.record_emptied(emptied)
            _reexport_playlists(reg, handle, removed_ids, playlists_dir)
            reg.finish("stopped" if reg.should_stop() else "done")
    except LibraryRootUnavailableError as exc:
        reg.fail(str(exc))
    except Exception as exc:  # any crash becomes a failed job, never a lost thread
        _log.exception("disk sync crashed")
        reg.fail(str(exc) or exc.__class__.__name__)
    finally:
        if on_complete is not None:
            on_complete()


def _reexport_playlists(
    reg: DiskSyncRegistry,
    handle: LibraryHandle,
    removed_ids: set[int],
    playlists_dir: Path | None,
) -> None:
    """Repair the `.m3u8` of every playlist this run dropped a track from.

    Best-effort and isolated: collateral must never turn a completed sync into a
    FAILED job via ``sweep``'s blanket handler — the rows really are gone.
    """
    if playlists_dir is None or not removed_ids:
        return
    try:
        reg.record_playlists_reexported(
            reexport_playlists_containing_sync(removed_ids, handle.lib, playlists_dir)
        )
    except Exception:
        _log.warning("disk sync .m3u8 re-export pass failed", exc_info=True)


def start_backfill(
    reg: DiskSyncRegistry,
    handle: LibraryHandle,
    *,
    playlists_dir: Path | None = None,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Spawn the sync on a daemon thread (non-blocking).

    Via ``reg.spawn_worker`` so a refused ``Thread.start()`` frees the slot
    instead of wedging every library mutation (see SingleSlotRegistry)."""
    reg.spawn_worker(
        lambda: sweep(reg, handle, playlists_dir=playlists_dir, on_complete=on_complete),
        name="musicdrop-disk-sync",
    )
