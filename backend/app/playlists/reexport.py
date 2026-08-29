"""The `.m3u8` re-export collateral every library MOVER owes its playlists.

An export under ``<music>/.playlists`` embeds each track's path RELATIVE to the
export dir, so any operation that relocates a file — or drops its row — leaves
every export holding that track stale: pointing at a path MusicDrop itself just
changed or removed. The invariant is that no mover may leave one behind, so each
one calls :func:`reexport_playlists_containing_sync` with the item ids it
touched.

This module is the ONE implementation, with two entrances:

* the SYNC core here, called directly by the daemon worker threads (reorganize,
  disk sync, the import worker) that have no event loop to await on;
* ``app.api.playlists.reexport_playlists_containing``, a thin
  ``run_in_threadpool`` wrapper for the request-path callers.

Layering: it sits ABOVE ``app.beets.playlists`` (which owns every beets read the
export needs) and ``app.playlists.store``/``.m3u``, and BELOW ``app.api`` — a
worker thread must be able to reach it without importing a router. It performs
no beets access of its own beyond the library's configured music root, so the
adapter boundary (CLAUDE.md rule 3) stays intact.

Best-effort throughout: the owned playlist store is the source of truth, so a
filesystem hiccup here must never fail the mutation that triggered it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi.concurrency import run_in_threadpool

from app.beets.library import LibraryHandle
from app.beets.playlists import m3u_entries
from app.config import settings
from app.playlists import store
from app.playlists.m3u import write_m3u
from app.playlists.store import StoredPlaylist
from app.wire import wire_safe

logger = logging.getLogger(__name__)


def export_dir_for(lib: Any) -> Path:  # lib: beets Library, untyped at the adapter boundary
    """Where the `.m3u8` exports live: the configured dir, else ``<music>/.playlists``."""
    configured = settings.playlists_export_dir.strip()
    if configured:
        return Path(configured)
    return Path(os.fsdecode(lib.directory)) / ".playlists"


def render_export(
    record: StoredPlaylist,
    lib: Any,  # beets Library, untyped at the adapter boundary
    export_dir: Path,
) -> None:
    """Write one playlist's `.m3u8`. Raises on a filesystem failure — see
    :func:`export_playlist` for the swallowing entrance."""
    entries = m3u_entries(lib, record.resolved_item_ids, str(export_dir))
    # The NAME is a display label, so it gets the wire treatment (surrogates ->
    # U+FFFD) before rendering: the store keeps a client-sent lone surrogate
    # losslessly, and a HIGH one (outside surrogateescape's window) would kill
    # the whole best-effort export that track PATHS — the locators, written
    # byte-exact — depend on. Lossy on the label, never on the locator.
    write_m3u(export_dir / f"{record.id}.m3u8", wire_safe(record.name), entries)


def export_playlist(
    record: StoredPlaylist,
    lib: Any,  # beets Library, untyped at the adapter boundary
    export_dir: Path,
) -> None:
    """Best-effort `.m3u8` (re)write — never raises.

    The owned store is the source of truth, so a filesystem hiccup must never
    fail the mutation that asked for the export. Logged, not propagated.
    """
    try:
        render_export(record, lib, export_dir)
    except Exception:
        logger.warning("Playlist .m3u8 export failed for %s", record.id, exc_info=True)


def reexport_playlists_containing_sync(
    item_ids: set[int],
    lib: Any,  # beets Library, untyped at the adapter boundary
    playlists_dir: Path,
) -> int:
    """Re-export the `.m3u8` of every stored playlist holding any of ``item_ids``.

    ``item_ids`` are the items the caller MOVED or REMOVED. A moved item gets a
    fresh relative path; a removed one simply stops resolving, so
    ``m3u_entries`` drops its line — one pass repairs both.

    Best-effort per playlist (:func:`export_playlist` never raises); returns how
    many playlists were re-exported, counting a failed write as an attempt so
    the number matches "playlists this operation touched". An empty id set
    returns 0 WITHOUT reading the store.

    Blocking (store IO + beets reads + file writes): call it on a worker thread,
    never on the event loop.
    """
    if not item_ids:
        return 0
    records = store.list_playlists(playlists_dir)
    export_dir = export_dir_for(lib)
    count = 0
    for record in records:
        if any(iid in item_ids for iid in record.resolved_item_ids):
            export_playlist(record, lib, export_dir)
            count += 1
    return count


async def reexport_playlists_containing(
    item_ids: set[int], handle: LibraryHandle, playlists_dir: Path
) -> int:
    """The request-path entrance: :func:`reexport_playlists_containing_sync` off-thread.

    Same semantics and same return value — it exists only so a coroutine caller
    does not block the event loop on store IO plus N file writes. Delegating (not
    re-deriving) is what keeps the two entrances from drifting.
    """
    return await run_in_threadpool(
        reexport_playlists_containing_sync, item_ids, handle.lib, playlists_dir
    )
