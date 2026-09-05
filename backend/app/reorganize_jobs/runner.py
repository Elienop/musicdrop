"""The reorganize worker — a sequential library/artist/album sweep (the `beet move`
analog). ``sweep`` is the synchronous, directly-testable loop; ``start_backfill``
runs it on a daemon thread so the API start endpoint returns immediately. Pure
local file IO, so it runs flat out: nothing here is rate-limited."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from app.beets.library import LibraryHandle, library_paths_context
from app.beets.orphans import find_orphan_folders
from app.beets.protected import ProtectedTreeError, protected_trees
from app.beets.reorganize import (
    collect_units,
    live_album_roots,
    reorganize_album,
    reorganize_singleton,
)
from app.beets.store_layout import (
    StoreLayoutError,
    check_store_layout,
    lib_music_and_library,
)
from app.beets.trash import trash_folder
from app.beets.trash_origins import TrashOriginsStoreUnusableError, require_usable_store
from app.config import Settings
from app.config import settings as module_settings
from app.models.reorganize import ReorganizeOutcome, ReorganizeScope
from app.playlists.reexport import reexport_playlists_containing_sync
from app.reorganize_jobs.registry import ReorganizeRegistry

_log = logging.getLogger(__name__)


def sweep(
    reg: ReorganizeRegistry,
    handle: LibraryHandle,
    *,
    scope: ReorganizeScope,
    artist: str | None = None,
    album_id: int | None = None,
    trash_dir: Path | None = None,
    trash_origins_dir: Path | None = None,
    ignore_dirs: tuple[Path, ...] = (),
    playlists_dir: Path | None = None,
    settings: Settings | None = None,
    reorg_album: Callable[..., ReorganizeOutcome] = reorganize_album,
    reorg_singleton: Callable[..., ReorganizeOutcome] = reorganize_singleton,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """Run the scoped sweep to completion. Never raises.

    ``on_complete`` fires once on termination (done/stopped/fail) — a partial
    run still moved files, so open tabs should refetch.

    When ``trash_dir`` is given, an orphan pass runs after the moves: any
    audio-empty husk left behind is moved to Trash. Omit it (the default) and
    the pass is skipped, leaving existing callers unchanged. ``trash_origins_dir``
    is wired as a PAIR with it (both come from one resolve in the API layer) and
    is what lets a relocated husk be put back exactly — the ONLY exit from Trash
    an audio-free folder has, so the pass is skipped unless both are present
    rather than trashing husks that could never be restored.

    When ``playlists_dir`` is given, a `.m3u8` re-export pass runs LAST, over the
    union of every item the run actually relocated — a reorganize is the widest
    mover there is, and every export holding one of those tracks now names a path
    that no longer exists. It runs on a STOP too (a stopped run still moved
    files) and before ``reg.finish``, so the terminal status already carries the
    count. Omit it and the pass is skipped, leaving existing callers unchanged.

    ``settings`` is the route's own instance, threaded in the way the two store
    directories are: the orphan pass asks the layout rule and the identity guard
    the same questions the route asked before it spawned this run. It falls back
    to the module singleton for the callers that pass nothing.
    """
    try:
        with library_paths_context(handle):
            albums, singletons = collect_units(
                handle.lib, scope=scope, artist=artist, album_id=album_id
            )
            reg.set_total(len(albums) + len(singletons))
            vacated: list[Path] = []
            moved_ids: set[int] = set()
            # ``_sweep_units`` no longer finishes the job itself: the re-export
            # tail below has to run on the stopped path too, and it must land
            # BEFORE the phase flips (a status read of a `done` job must not
            # still be missing its count). One owner for every finish, here.
            stopped = _sweep_units(
                reg,
                handle.lib,
                albums,
                reorg_album,
                vacated=vacated,
                moved_ids=moved_ids,
            )
            if not stopped:
                stopped = _sweep_units(
                    reg,
                    handle.lib,
                    singletons,
                    reorg_singleton,
                    vacated=vacated,
                    moved_ids=moved_ids,
                )
            if not stopped and trash_dir is not None and trash_origins_dir is not None:
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
                    handle,
                    scope=scope,
                    music_dir=Path(os.fsdecode(handle.lib.directory)),
                    trash_dir=trash_dir,
                    trash_origins_dir=trash_origins_dir,
                    vacated=vacated,
                    ignore_dirs=ignore_dirs,
                    protected_dirs=live_album_roots(handle.lib),
                    settings=settings,
                )
            _reexport_playlists(reg, handle, moved_ids, playlists_dir)
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
    moved_ids: set[int],
) -> bool:
    """Sweep one unit list (albums or singletons), the runner's twin loops.

    Per unit: the outcome is recorded via ``reg.record`` and then
    ``reg.set_current(outcome.label)``; the vacated source dir (when set) is
    appended to ``vacated``; every item the unit actually relocated is added to
    ``moved_ids`` (a FAILED unit contributes too — see ``ReorganizeOutcome``).
    Returns ``True`` on a Stop request WITHOUT
    finishing the job — ``sweep`` owns every ``reg.finish`` so the `.m3u8` tail
    pass still runs on the stopped path."""
    for unit in units:
        if reg.should_stop():
            return True
        outcome = reorg(lib, unit)
        reg.record(outcome)
        reg.set_current(outcome.label)
        if outcome.source_dir:
            vacated.append(Path(outcome.source_dir))
        moved_ids.update(outcome.moved_item_ids)
    return False


def _reexport_playlists(
    reg: ReorganizeRegistry,
    handle: LibraryHandle,
    moved_ids: set[int],
    playlists_dir: Path | None,
) -> None:
    """Repair the `.m3u8` exports of every playlist this run moved a track out of.

    Best-effort and isolated: this is collateral, so a store or filesystem fault
    here must be logged, never turned into a FAILED reorganize by ``sweep``'s
    blanket handler — the files really did move.
    """
    if playlists_dir is None or not moved_ids:
        return
    try:
        reg.record_playlists_reexported(
            reexport_playlists_containing_sync(moved_ids, handle.lib, playlists_dir)
        )
    except Exception:
        _log.warning("reorganize .m3u8 re-export pass failed", exc_info=True)


def _sweep_orphans(
    reg: ReorganizeRegistry,
    handle: LibraryHandle,
    *,
    scope: ReorganizeScope,
    music_dir: Path,
    trash_dir: Path,
    trash_origins_dir: Path,
    vacated: list[Path],
    ignore_dirs: tuple[Path, ...],
    protected_dirs: Collection[str],
    settings: Settings | None = None,
) -> bool:
    """Move audio-empty husks to Trash. Library scope scans the whole root; a
    narrower scope seeds from the dirs this run vacated. Per-folder failures are
    isolated so one bad move never aborts the job. Returns True if it broke early
    on a Stop request (so the caller finishes ``stopped``, not ``done``).

    ``protected_dirs`` are the live albums' own dirs: a seeded climb lands on an
    album's audio-free subfolder just as readily as a library scan does, so both
    modes get the same set.

    The origin store is asked ONCE, up front, and a store that cannot be used
    skips the whole phase with a WARNING instead of failing the job. Per-folder
    it would refuse identically for every husk, and the ``except OSError``
    below — written to isolate one bad folder — would swallow every one of them
    in silence. Failing the job instead would cost the run its `.m3u8` re-export
    tail (``sweep``'s blanket handler calls ``reg.fail`` and skips it) for a
    fault that has nothing to do with the files this run already moved."""
    # Asked HERE and not only at boot: this phase runs on a worker thread minutes
    # after the request that started it, and both roots are re-resolved per use,
    # so a symlink that appeared at the Trash path in between would send every
    # husk somewhere the boot check had approved of a different directory. Same
    # WARNING-and-skip as the store guard below, and for the same reason: the
    # move phase has already relocated real files, and failing the job here would
    # cost the run its .m3u8 re-export tail for a fault about the Trash.
    music_root, library_path = lib_music_and_library(handle.lib)
    # The route's own ``Settings`` when it threaded one in, so the layout this
    # phase checks and the ignore list the route built come from ONE object.
    # ``app.state.settings`` is the monkeypatch surface, and reading the module
    # global here let the two name different instances.
    store_settings = module_settings if settings is None else settings
    try:
        check_store_layout(
            music_dir=music_root,
            beets_dir=handle.beets_dir,
            trash_dir=trash_dir,
            origins_dir=trash_origins_dir,
            library_path=library_path,
            settings=store_settings,
        )
    except StoreLayoutError:
        _log.warning("orphan sweep skipped: the store layout is refused", exc_info=True)
        return False
    # Beside the layout check, from the pair it just approved: the spelled rows
    # above miss an alias, so each candidate is asked again by inode below.
    protected = protected_trees(
        settings=store_settings,
        music_dir=music_root,
        beets_dir=handle.beets_dir,
        trash_dir=trash_dir,
        origins_dir=trash_origins_dir,
        library_path=library_path,
    )
    try:
        require_usable_store(trash_origins_dir)
    except TrashOriginsStoreUnusableError:
        _log.warning("orphan sweep skipped: the Trash origin store cannot be used", exc_info=True)
        return False
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
            trash_folder(
                folder, trash_dir=trash_dir, origins_dir=trash_origins_dir, protected=protected
            )
            reg.record_orphans(1)
        except ProtectedTreeError as exc:
            # Its own arm above ``OSError``, which is not a supertype of it: a
            # candidate that is or holds one of the app's own directories is
            # skipped with a WARNING, like an unusable store, so the rest of the
            # pass still runs.
            _log.warning("orphan sweep skipped a folder: %s", exc)
            continue
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
    trash_origins_dir: Path | None = None,
    ignore_dirs: tuple[Path, ...] = (),
    playlists_dir: Path | None = None,
    settings: Settings | None = None,
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
            trash_origins_dir=trash_origins_dir,
            ignore_dirs=ignore_dirs,
            playlists_dir=playlists_dir,
            settings=settings,
            on_complete=on_complete,
        ),
        name="musicdrop-reorganize",
    )
