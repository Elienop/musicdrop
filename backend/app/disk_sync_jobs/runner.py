"""The disk-sync worker: one library-wide walk (the ``beet update`` analog).
``sweep`` is the synchronous, directly-testable body; ``start_backfill`` runs
it on a daemon thread so the API start endpoint returns immediately."""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.beets.disk_sync import LibraryRootUnavailableError, run_disk_sync
from app.beets.library import LibraryHandle, library_paths_context
from app.disk_sync_jobs.registry import DiskSyncRegistry


def sweep(
    reg: DiskSyncRegistry,
    handle: LibraryHandle,
    *,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Run the sync to completion. Never raises.

    ``on_complete`` fires once on termination (done/stopped/failed) — a partial
    run still changed the DB, so open tabs should refetch.
    """
    try:
        with library_paths_context(handle):
            emptied = run_disk_sync(
                handle.lib,
                on_total=reg.set_total,
                on_item=reg.record,
                should_stop=reg.should_stop,
            )
            reg.record_emptied(emptied)
            reg.finish("stopped" if reg.should_stop() else "done")
    except LibraryRootUnavailableError as exc:
        reg.fail(str(exc))
    except Exception as exc:  # any crash becomes a failed job, never a lost thread
        reg.fail(str(exc) or exc.__class__.__name__)
    finally:
        if on_complete is not None:
            on_complete()


def start_backfill(
    reg: DiskSyncRegistry,
    handle: LibraryHandle,
    *,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Spawn the sync on a daemon thread (non-blocking)."""
    threading.Thread(
        target=lambda: sweep(reg, handle, on_complete=on_complete),
        name="musicdrop-disk-sync",
        daemon=True,
    ).start()
