"""Sync-with-disk adapter: the ``beet update`` equivalent (disk -> DB, one way).

Faithful reimplementation of beets 2.12 ``update_items``
(beets/ui/commands/update.py) minus terminal printing and minus ANY file
moves/writes/deletes:

* missing file      -> remove the item row (``delete=False`` always — even the
                       already-gone file is never touched; ``with_album=True``
                       auto-prunes an album losing its last item)
* mtime unchanged   -> skip (``current_mtime() <= item.mtime``)
* mtime newer       -> re-read tags; beets' albumartist special case preserved
* affected albums   -> realign Album.item_keys from the first remaining item

beets imports are allowed here (app/beets/, CLAUDE.md rule 3). Every entry
point binds ``lib.music_dir_context()`` so relative DB paths resolve on worker
threads.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from beets import library
from beets.library import ReadError

from app.beets.library import LibraryRootUnavailableError as LibraryRootUnavailableError
from app.beets.library import _music_dir, require_library_root
from app.beets.reorganize import PREVIEW_ROW_CAP
from app.models.disk_sync import (
    DiskSyncChange,
    DiskSyncEmptiedAlbum,
    DiskSyncOutcome,
    DiskSyncPlan,
    DiskSyncReadError,
    DiskSyncRemoval,
)

#: The root guard now lives in the base adapter (``app.beets.library``) because
#: the Trash primitives need the SAME predicate — a second, looser copy is the
#: failure that placement prevents. Two names survive here on purpose:
#:
#: * the ``as``-aliased re-export above keeps ``disk_sync.LibraryRootUnavailableError``
#:   a module ATTRIBUTE (explicit re-export, so mypy --strict's
#:   no-implicit-reexport and ruff's F401 both stay quiet), which is how
#:   ``tests/test_disk_sync_adapter.py`` reaches it through the module;
#: * ``_require_root`` is the module-global the call sites below resolve through,
#:   and therefore the seam that same test monkeypatches to simulate a mount
#:   dropping mid-sweep.
_require_root = require_library_root


def _item_label(item: Any) -> str:
    artist = str(item.artist or item.albumartist or "").strip() or "Unknown"
    title = str(item.title or "").strip() or os.path.basename(os.fsdecode(item.path))
    return f"{artist} - {title}"


def _album_label(album: Any) -> str:
    artist = str(getattr(album, "albumartist", "") or "").strip() or "Unknown"
    return f"{artist} - {album.album}"


def _rel_path(lib: Any, item: Any) -> str:
    path = os.fsdecode(item.path)
    root = _music_dir(lib)
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _rel_dir(lib: Any, item: Any) -> str:
    """The folder holding ``item``, relative to the music dir (display only)."""
    return os.path.dirname(_rel_path(lib, item)) or "."


def _common_rel_dir(first: str, other: str) -> str:
    """Deepest folder shared by two item dirs (the album root for multi-disc).

    commonpath over the REL dirs — not full paths — so the result stays a
    display-relative path. POSIX commonpath treats "." (a file at the library
    root) as the root itself and yields ``""`` for it; ``or "."`` keeps the
    "." display. An escaping relpath (a file outside the music dir) may
    render as-is; the rare ValueError (mixed roots) falls back to the dir
    seen first.
    """
    try:
        return os.path.commonpath([first, other]) or "."
    except ValueError:
        return first


def _file_missing(item: Any) -> bool:
    return not item.path or not os.path.exists(item.path)


def _probe_changed_fields(item: Any) -> list[str]:
    """Media fields whose on-disk value differs from the DB row.

    Reads the file into a DETACHED ``Item.from_path`` probe (never the DB row
    itself), so callers stay read-only. Mirrors beets' albumartist special
    case: an empty probe albumartist is NOT a change when the old row had
    ``albumartist == artist`` and the artist itself is unchanged (update.py's
    "hacky but necessary" branch). Raises ``ReadError`` for unreadable files.
    """
    probe = library.Item.from_path(item.path)
    changed = [f for f in library.Item._media_fields if probe.get(f) != item.get(f)]
    if (
        "albumartist" in changed
        and not probe.albumartist
        and item.albumartist == item.artist == probe.artist
    ):
        changed.remove("albumartist")
    return sorted(changed)


class _PlanAccumulator:
    """Per-sweep running totals for a plan dry run (see ``plan_disk_sync``)."""

    def __init__(self) -> None:
        self.removals: list[DiskSyncRemoval] = []
        self.changes: list[DiskSyncChange] = []
        self.read_errors: list[DiskSyncReadError] = []
        self.will_remove = 0
        self.will_update = 0
        self.album_totals: dict[int, int] = {}
        self.album_missing: dict[int, int] = {}
        self.album_dirs: dict[int, str] = {}


def _fold_album_membership(lib: Any, item: Any, acc: _PlanAccumulator) -> int | None:
    """Fold the item into its album's totals and root dir; returns the album id."""
    album_id: int | None = item.album_id
    if album_id is None:
        return None
    acc.album_totals[album_id] = acc.album_totals.get(album_id, 0) + 1
    rel_dir = _rel_dir(lib, item)
    if album_id in acc.album_dirs:
        # Items can span folders (a disc-bearing ``paths:`` template or a
        # mid-reorganize crash split): fold toward the deepest common
        # dir so the row shows the album ROOT, not the first disc's.
        acc.album_dirs[album_id] = _common_rel_dir(acc.album_dirs[album_id], rel_dir)
    else:
        acc.album_dirs[album_id] = rel_dir
    return album_id


def _scan_item(lib: Any, item: Any, acc: _PlanAccumulator) -> None:
    """Classify one item (remove / update / skip) and fold it into ``acc``."""
    album_id = _fold_album_membership(lib, item, acc)
    if _file_missing(item):
        acc.will_remove += 1
        if album_id is not None:
            acc.album_missing[album_id] = acc.album_missing.get(album_id, 0) + 1
        if len(acc.removals) < PREVIEW_ROW_CAP:
            acc.removals.append(DiskSyncRemoval(label=_item_label(item), path=_rel_path(lib, item)))
        return
    if item.current_mtime() <= item.mtime:
        return
    try:
        fields = _probe_changed_fields(item)
    except ReadError as exc:
        if len(acc.read_errors) < PREVIEW_ROW_CAP:
            acc.read_errors.append(DiskSyncReadError(label=_item_label(item), error=str(exc)))
        return
    if fields:
        acc.will_update += 1
        if len(acc.changes) < PREVIEW_ROW_CAP:
            acc.changes.append(DiskSyncChange(label=_item_label(item), fields=fields))


def _collect_emptied(
    lib: Any, acc: _PlanAccumulator
) -> tuple[list[int], list[DiskSyncEmptiedAlbum]]:
    """Album ids whose every item is gone, plus their capped preview rows."""
    emptied_ids = [
        aid for aid, total in acc.album_totals.items() if acc.album_missing.get(aid, 0) == total
    ]
    emptied: list[DiskSyncEmptiedAlbum] = []
    for aid in emptied_ids[:PREVIEW_ROW_CAP]:
        album = lib.get_album(aid)
        if album is not None:
            # Count and folder come from THIS row's own items (album_totals /
            # album_dirs are keyed by album_id), never from a same-named twin.
            emptied.append(
                DiskSyncEmptiedAlbum(
                    label=_album_label(album),
                    track_count=acc.album_totals[aid],
                    path=acc.album_dirs[aid],
                )
            )
    return emptied_ids, emptied


def _build_plan(
    total_items: int,
    acc: _PlanAccumulator,
    emptied_ids: list[int],
    emptied: list[DiskSyncEmptiedAlbum],
) -> DiskSyncPlan:
    """Assemble the ``DiskSyncPlan`` from the accumulator and emptied rows."""
    truncated = (
        acc.will_remove > len(acc.removals)
        or acc.will_update > len(acc.changes)
        or len(emptied_ids) > len(emptied)
    )
    return DiskSyncPlan(
        total_items=total_items,
        will_remove=acc.will_remove,
        will_update=acc.will_update,
        emptied_albums=emptied,
        emptied_total=len(emptied_ids),
        removals=acc.removals,
        changes=acc.changes,
        read_errors=acc.read_errors,
        truncated=truncated,
    )


def plan_disk_sync(lib: Any) -> DiskSyncPlan:
    """Read-only dry run: what a sync would remove/update. Mutates NOTHING."""
    with lib.music_dir_context():
        _require_root(lib)
        items = list(lib.items())
        acc = _PlanAccumulator()
        for item in items:
            _scan_item(lib, item, acc)
        emptied_ids, emptied = _collect_emptied(lib, acc)
        return _build_plan(len(items), acc, emptied_ids, emptied)


def _sync_item(
    lib: Any,
    item: Any,
    media_fields: set[str],
    store_fields: set[str],
    affected: set[int],
    on_item: Callable[[DiskSyncOutcome], None],
) -> None:
    """Apply the sync to one item and emit its ``DiskSyncOutcome``."""
    label = _item_label(item)
    if _file_missing(item):
        # Re-verify the root before treating a missing file as a deletion.
        # _require_root ran once at the start, but if the share unmounts
        # mid-sweep EVERY remaining file looks gone and the loop would wipe
        # thousands of DB rows in one pass. A dropped mount fails this check
        # and aborts (raising past run_disk_sync) — genuine deletions on a
        # still-mounted root fall through and remove as before.
        _require_root(lib)
        if item.album_id is not None:
            affected.add(int(item.album_id))
        item.remove(delete=False, with_album=True)
        on_item(DiskSyncOutcome(status="removed", label=label))
        return
    if item.current_mtime() <= item.mtime:
        on_item(DiskSyncOutcome(status="unchanged", label=label))
        return
    old_albumartist = item.albumartist
    old_artist = item.artist
    try:
        item.read()
    except ReadError as exc:
        on_item(DiskSyncOutcome(status="read_error", label=label, error=str(exc)))
        return
    # beets' albumartist special case (update.py): an empty re-read
    # albumartist is not a change when the old row had
    # albumartist == artist and the artist itself is unchanged.
    if not item.albumartist and old_albumartist == old_artist == item.artist:
        item.albumartist = old_albumartist
        item._dirty.discard("albumartist")
    changed = sorted(f for f in item._dirty if f in media_fields)
    # Store either way: even with no tag change this persists the new
    # mtime (set by read()) so the item is not re-checked forever —
    # exactly beets' no-change branch. mtime must be in store_fields; it
    # is deliberately excluded from `changed` so it never counts as a
    # tag change.
    item.store(fields=store_fields)
    if changed:
        if item.album_id is not None:
            affected.add(int(item.album_id))
        on_item(DiskSyncOutcome(status="updated", label=label, fields=changed))
    else:
        on_item(DiskSyncOutcome(status="unchanged", label=label))


def _realign_albums(lib: Any, affected: set[int]) -> int:
    """Realign affected albums from their first remaining item; return pruned count."""
    emptied = 0
    for album_id in affected:
        album = lib.get_album(album_id)
        if album is None:
            emptied += 1  # pruned by the last item.remove(with_album=True)
            continue
        first_item = album.items().get()
        if first_item is None:
            continue
        for key in library.Album.item_keys:
            album[key] = first_item[key]
        # inherit=False — beets' default (inherit=True) would push these
        # album-level values down onto EVERY track row, clobbering
        # per-track values that legitimately differ file-to-file, and
        # beets zeroes each touched track's mtime ("Reset mtime on
        # dirty"), reopening the gate so the same tracks re-sync forever.
        # Deliberate deviation from beets' update.py (which has this same
        # churn): disk-sync promises each row mirrors ITS OWN file.
        album.store(inherit=False)
    return emptied


def run_disk_sync(
    lib: Any,
    *,
    on_total: Callable[[int], None],
    on_item: Callable[[DiskSyncOutcome], None],
    should_stop: Callable[[], bool],
) -> int:
    """Apply the sync (beets ``update_items`` semantics, no printing, no moves).

    Emits one ``DiskSyncOutcome`` per processed item; honors ``should_stop``
    between items (already-applied work stands — the sync is idempotent and a
    re-run is cheap thanks to the mtime gate). Returns the number of albums
    pruned (emptied). Never touches a file: removals use ``delete=False`` and
    nothing is ever moved or written to disk.
    """
    with lib.music_dir_context():
        _require_root(lib)
        items = list(lib.items())
        on_total(len(items))
        affected: set[int] = set()
        media_fields = library.Item._media_fields
        # Field-limited store protects DB-only flex fields (e.g. lyrics_checked)
        # from being clobbered by the re-read. mtime is NOT a media field, so it
        # must be added explicitly — otherwise store() drops the fresh mtime and
        # the gate never shuts, re-reading every touched file on every sync.
        store_fields = media_fields | {"mtime"}
        for item in items:
            if should_stop():
                break
            _sync_item(lib, item, media_fields, store_fields, affected, on_item)
        return _realign_albums(lib, affected)
