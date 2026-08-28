"""The reorganize worker — a sequential library/artist/album sweep (the `beet move`
analog). ``sweep`` is the synchronous, directly-testable loop; ``start_backfill``
runs it on a daemon thread so the API start endpoint returns immediately. Pure
local file IO, so no courtesy delay is needed (default 0)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from app.beets.library import LibraryHandle, library_paths_context
from app.beets.orphans import find_orphan_folders
from app.beets.reorganize import (
    collect_units,
    live_album_roots,
    reorganize_album,
    reorganize_singleton,
)
from app.beets.trash import trash_folder
from app.models.reorganize import ReorganizeOutcome, ReorganizeScope
from app.reorganize_jobs.registry import ReorganizeRegistry


def sweep(
    reg: ReorganizeRegistry,
    handle: LibraryHandle,
    *,
    scope: ReorganizeScope,
    artist: str | None = None,
    album_id: int | None = None,
    trash_dir: Path | None = None,
    ignore_dirs: tuple[Path, ...] = (),
    delay: float = 0.0,
    reorg_album: Callable[..., ReorganizeOutcome] = reorganize_album,
    reorg_singleton: Callable[..., ReorganizeOutcome] = reorganize_singleton,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Run the scoped sweep to completion. Never raises.

    ``on_complete`` fires once on termination (done/stopped/fail) — a partial
    run still moved files, so open tabs should refetch.

    When ``trash_dir`` is given, an orphan pass runs after the moves: any
    audio-empty husk left behind is moved to Trash. Omit it (the default) and
    the pass is skipped, leaving existing callers unchanged.
    """
    try:
        with library_paths_context(handle):
            albums, singletons = collect_units(
                handle.lib, scope=scope, artist=artist, album_id=album_id
            )
            reg.set_total(len(albums) + len(singletons))
            vacated: list[Path] = []
            if _sweep_units(reg, handle.lib, albums, reorg_album, vacated=vacated, delay=delay):
                return
            if _sweep_units(
                reg,
                handle.lib,
                singletons,
                reorg_singleton,
                vacated=vacated,
                delay=delay,
            ):
                return
            stopped = False
            if trash_dir is not None:
                # Read AFTER the unit loops, because the roots move during the
                # run. What keeps the library still between this read and the
                # orphan pass is the library-busy UNION gate (app/library_busy.py,
                # which consults this job's slot): a delete, a duplicate resolve
                # or an import is refused while a reorganize runs. The single-slot
                # registry alone only excludes a SECOND reorganize.
                # Inside this branch on purpose: a caller that skips the orphan
                # phase must not pay for the DB pass.
                stopped = _sweep_orphans(
                    reg,
                    scope=scope,
                    music_dir=Path(os.fsdecode(handle.lib.directory)),
                    trash_dir=trash_dir,
                    vacated=vacated,
                    ignore_dirs=ignore_dirs,
                    protected_dirs=live_album_roots(handle.lib),
                )
            reg.finish("stopped" if stopped else "done")
    except Exception as exc:  # any crash becomes a failed job, never a lost thread
        reg.fail(str(exc) or exc.__class__.__name__)
    finally:
        if on_complete is not None:
            on_complete()


def _sweep_units(
    reg: ReorganizeRegistry,
    lib: Any,  # beets library object (untyped, adapter boundary)
    units: list[Any],  # beets albums/singletons (untyped, adapter boundary)
    reorg: Callable[..., ReorganizeOutcome],
    *,
    vacated: list[Path],
    delay: float,
) -> bool:
    """Sweep one unit list (albums or singletons), the runner's twin loops.

    Per unit: the outcome is recorded via ``reg.record`` and then
    ``reg.set_current(outcome.label)``; the vacated source dir (when set)
    is appended to ``vacated``; the courtesy delay is honored. On a Stop
    request, ``reg.finish("stopped")`` is called and ``True`` is returned, so
    the caller must not fall through to the tail."""
    for unit in units:
        if reg.should_stop():
            reg.finish("stopped")
            return True
        outcome = reorg(lib, unit)
        reg.record(outcome)
        reg.set_current(outcome.label)
        if outcome.source_dir:
            vacated.append(Path(outcome.source_dir))
        if delay:
            time.sleep(delay)
    return False


def _sweep_orphans(
    reg: ReorganizeRegistry,
    *,
    scope: ReorganizeScope,
    music_dir: Path,
    trash_dir: Path,
    vacated: list[Path],
    ignore_dirs: tuple[Path, ...],
    protected_dirs: Collection[str],
) -> bool:
    """Move audio-empty husks to Trash. Library scope scans the whole root; a
    narrower scope seeds from the dirs this run vacated. Per-folder failures are
    isolated so one bad move never aborts the job. Returns True if it broke early
    on a Stop request (so the caller finishes ``stopped``, not ``done``).

    ``protected_dirs`` are the live albums' own dirs: a seeded climb lands on an
    album's audio-free subfolder just as readily as a library scan does, so both
    modes get the same set."""
    seeds = None if scope == "library" else vacated
    for folder in find_orphan_folders(
        music_dir,
        seeds=seeds,
        trash_dir=trash_dir,
        ignore_dirs=ignore_dirs,
        protected_dirs=protected_dirs,
    ):
        if reg.should_stop():
            return True
        try:
            trash_folder(folder, trash_dir=trash_dir)
            reg.record_orphans(1)
        except OSError:
            continue
    return False


def start_backfill(
    reg: ReorganizeRegistry,
    handle: LibraryHandle,
    *,
    scope: ReorganizeScope,
    artist: str | None = None,
    album_id: int | None = None,
    trash_dir: Path | None = None,
    ignore_dirs: tuple[Path, ...] = (),
    delay: float = 0.0,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Spawn the scoped sweep on a daemon thread (non-blocking).

    Via ``reg.spawn_worker`` so a refused ``Thread.start()`` frees the slot
    instead of wedging every library mutation (see SingleSlotRegistry)."""
    reg.spawn_worker(
        lambda: sweep(
            reg,
            handle,
            scope=scope,
            artist=artist,
            album_id=album_id,
            trash_dir=trash_dir,
            ignore_dirs=ignore_dirs,
            delay=delay,
            on_complete=on_complete,
        ),
        name="musicdrop-reorganize",
    )
