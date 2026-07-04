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

from app.beets.reorganize import PREVIEW_ROW_CAP
from app.models.disk_sync import (
    DiskSyncChange,
    DiskSyncOutcome,
    DiskSyncPlan,
    DiskSyncReadError,
    DiskSyncRemoval,
)


class LibraryRootUnavailableError(Exception):
    """The music directory itself is missing — likely an unmounted share.

    Guard against the nightmare scenario: with the root gone EVERY file looks
    deleted and a sync would wipe the whole DB. Fail fast instead.
    """


def _music_dir(lib: Any) -> str:
    return os.path.normpath(os.fsdecode(lib.directory))


def _require_root(lib: Any) -> None:
    if not os.path.isdir(_music_dir(lib)):
        raise LibraryRootUnavailableError(
            "Library folder unavailable — is the music share mounted?"
        )


def _item_label(item: Any) -> str:
    artist = str(item.artist or item.albumartist or "").strip() or "Unknown"
    title = str(item.title or "").strip() or os.path.basename(os.fsdecode(item.path))
    return f"{artist} — {title}"


def _album_label(album: Any) -> str:
    artist = str(getattr(album, "albumartist", "") or "").strip() or "Unknown"
    return f"{artist} — {album.album}"


def _rel_path(lib: Any, item: Any) -> str:
    path = os.fsdecode(item.path)
    root = _music_dir(lib)
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


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


def plan_disk_sync(lib: Any) -> DiskSyncPlan:
    """Read-only dry run: what a sync would remove/update. Mutates NOTHING."""
    with lib.music_dir_context():
        _require_root(lib)
        items = list(lib.items())
        removals: list[DiskSyncRemoval] = []
        changes: list[DiskSyncChange] = []
        read_errors: list[DiskSyncReadError] = []
        will_remove = 0
        will_update = 0
        album_totals: dict[int, int] = {}
        album_missing: dict[int, int] = {}
        for item in items:
            album_id = item.album_id
            if album_id is not None:
                album_totals[album_id] = album_totals.get(album_id, 0) + 1
            if _file_missing(item):
                will_remove += 1
                if album_id is not None:
                    album_missing[album_id] = album_missing.get(album_id, 0) + 1
                if len(removals) < PREVIEW_ROW_CAP:
                    removals.append(
                        DiskSyncRemoval(label=_item_label(item), path=_rel_path(lib, item))
                    )
                continue
            if item.current_mtime() <= item.mtime:
                continue
            try:
                fields = _probe_changed_fields(item)
            except ReadError as exc:
                if len(read_errors) < PREVIEW_ROW_CAP:
                    read_errors.append(DiskSyncReadError(label=_item_label(item), error=str(exc)))
                continue
            if fields:
                will_update += 1
                if len(changes) < PREVIEW_ROW_CAP:
                    changes.append(DiskSyncChange(label=_item_label(item), fields=fields))
        emptied_ids = [
            aid for aid, total in album_totals.items() if album_missing.get(aid, 0) == total
        ]
        emptied_labels: list[str] = []
        for aid in emptied_ids[:PREVIEW_ROW_CAP]:
            album = lib.get_album(aid)
            if album is not None:
                emptied_labels.append(_album_label(album))
        truncated = (
            will_remove > len(removals)
            or will_update > len(changes)
            or len(emptied_ids) > len(emptied_labels)
        )
        return DiskSyncPlan(
            total_items=len(items),
            will_remove=will_remove,
            will_update=will_update,
            emptied_albums=emptied_labels,
            emptied_total=len(emptied_ids),
            removals=removals,
            changes=changes,
            read_errors=read_errors,
            truncated=truncated,
        )


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
            label = _item_label(item)
            if _file_missing(item):
                if item.album_id is not None:
                    affected.add(int(item.album_id))
                item.remove(delete=False, with_album=True)
                on_item(DiskSyncOutcome(status="removed", label=label))
                continue
            if item.current_mtime() <= item.mtime:
                on_item(DiskSyncOutcome(status="unchanged", label=label))
                continue
            old_albumartist = item.albumartist
            old_artist = item.artist
            try:
                item.read()
            except ReadError as exc:
                on_item(DiskSyncOutcome(status="read_error", label=label, error=str(exc)))
                continue
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
