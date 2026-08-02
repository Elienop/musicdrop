# backend/app/beets/reorganize.py
"""Reorganize: re-apply the live beets paths/replace config to EXISTING files.

All beets access for the feature lives here (rule 3). A preview is read-only
(``item.destination`` compared to ``item.path``, exactly as ``beet move`` filters);
the move (Task 4) is ``Album.move``/``Item.move`` with ``MoveOperation.MOVE``, which
relocates files + art and prunes the vacated dirs. Every op binds
``lib.music_dir_context()`` because beets 2.11 stores DB paths relative to the
library dir and re-expands them via a ContextVar a worker thread does not inherit.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any

import beets
from beets.util import FilesystemError, MoveOperation, prune_dirs

from app.beets.library import LibraryHandle
from app.beets.orphans import find_orphan_folders
from app.beets.sidecars import move_sidecars
from app.models.reorganize import (
    OrphanFolder,
    ReorganizeMove,
    ReorganizeOutcome,
    ReorganizePlan,
    ReorganizeScope,
)

_log = logging.getLogger(__name__)

#: Detailed preview rows are capped here; counts stay exact, truncated=True past it.
PREVIEW_ROW_CAP = 1000


def _albums_for_scope(
    lib: Any, *, scope: ReorganizeScope, artist: str | None, album_id: int | None
) -> list[Any]:
    from beets.dbcore.query import MatchQuery

    if scope == "album":
        album = lib.get_album(album_id) if album_id is not None else None
        return [album] if album is not None else []
    if scope == "artist":
        # Parameterized query (not f-string) so metacharacters in the name are safe.
        return list(lib.albums(MatchQuery("albumartist", artist)))
    return list(lib.albums())


def _singletons_for_scope(lib: Any, *, scope: ReorganizeScope) -> list[Any]:
    # Singletons (album_id IS NULL) only sweep at library scope.
    if scope == "library":
        return list(lib.items("singleton:true"))
    return []


def collect_units(
    lib: Any, *, scope: ReorganizeScope, artist: str | None, album_id: int | None
) -> tuple[list[Any], list[Any]]:
    """(albums, singletons) for the scope. The runner snapshots these."""
    return (
        _albums_for_scope(lib, scope=scope, artist=artist, album_id=album_id),
        _singletons_for_scope(lib, scope=scope),
    )


def _item_moves(lib: Any, item: Any) -> bool:
    """True iff this item's file would relocate — beets' own per-item filter."""
    return bool(item.path != item.destination(basedir=lib.directory))


def _verify_moves(pending: list[tuple[int, bytes, bytes]], after: dict[int, bytes]) -> list[str]:
    """Post-move verification: beets 2.12 silently skips a move whose source file is
    absent (models.py:1142) and silently diverts to a `.N`-suffixed name when the
    destination is occupied (unique_path) — both leave the unit eternally re-flagged
    by the preview. Returns one human-readable problem per affected file."""
    problems: list[str] = []
    for item_id, old_path, dest in pending:
        new_path = after.get(item_id, old_path)
        name = os.path.basename(os.fsdecode(old_path))
        if new_path == old_path:
            if not os.path.exists(old_path):
                problems.append(
                    f"{name}: file not found on disk at the library's recorded path"
                    "; fix the file name on disk or re-import the album"
                )
            else:
                problems.append(f"{name}: move did not take effect")
        elif new_path != dest:
            problems.append(
                f"{name}: computed filename is already taken by another track"
                f" (landed at {os.path.basename(os.fsdecode(new_path))!r})"
                "; two tracks share the same track number and title"
            )
    return problems


def _carry_sidecars(
    lib: Any, pending: list[tuple[int, bytes, bytes]], after: dict[int, bytes]
) -> None:
    """Move each relocated item's lyric sidecars to its new location, then re-prune.

    beets moves audio + album art and nothing else, so MusicDrop's own
    ``.lrc``/``.txt`` sidecars (the files Plex actually reads) stay in the vacated
    folder — which the post-run orphan sweep then classifies as an audio-empty
    husk and moves to Trash, losing the lyrics while the job reports success. The
    same stranding happens on an in-place rename, where there is no husk at all
    and the sidecar simply stops matching its track.

    Keyed off the ACTUAL landing path rather than the computed destination, so a
    collision-diverted ``.1`` file keeps its lyrics.

    The re-prune is not cosmetic: beets prunes the vacated dir DURING the move,
    while the sidecars are still sitting in it, so that prune is a no-op and the
    now-empty dir would outlive every future sweep (``find_orphan_folders``
    deliberately ignores empty dirs). Pruning again with beets' own arguments
    makes the on-disk result identical to a sidecar-free move. Never raises: the
    audio has already moved and the unit's outcome must stay truthful about it.
    """
    vacated: set[bytes] = set()
    for item_id, old_path, _dest in pending:
        # Default = the old path: an item beets silently skipped (source file
        # missing) has not moved, and ``move_sidecars`` no-ops on an unchanged
        # path, so its sidecars stay put. ONE gate for that, tested there.
        new_path = after.get(item_id, old_path)
        if move_sidecars(old_path, new_path):
            vacated.add(os.path.dirname(old_path))
    for directory in vacated:
        try:
            prune_dirs(directory, lib.directory, clutter=beets.config["clutter"].as_str_seq())
        except OSError:
            _log.warning("pruning vacated dir failed: %r", directory, exc_info=True)


def _commonpath_of_dirs(paths: list[bytes]) -> str:
    """The deepest directory shared by all of ``paths`` (the unit's album root).

    commonpath over the item DIRS yields the album root even for multi-disc
    layouts ($album/Disc N/...). Falls back to the first dir on the rare
    ValueError (mixed roots / empty)."""
    dirs = [os.path.dirname(os.fsdecode(p)) for p in paths]
    if not dirs:
        return ""
    try:
        return os.path.commonpath(dirs)
    except ValueError:
        return dirs[0]


def album_label(album: Any) -> str:
    artist = str(getattr(album, "albumartist", "") or "").strip() or "Unknown"
    return f"{artist} - {album.album}"


def singleton_label(item: Any) -> str:
    artist = str(item.artist or item.albumartist or "").strip() or "Unknown"
    return f"{artist} - {item.title}"


def album_scope_label(handle: LibraryHandle, album_id: int) -> str | None:
    """``album_label`` for an album id, or ``None`` when the album is missing.

    Typed lookup for the router's 404-or-start decision, so it never calls
    ``lib.get_album`` itself (rule 3). Scalar reads; no path expansion needed.
    """
    album = handle.lib.get_album(album_id)
    if album is None:
        return None
    return album_label(album)


def _describe_album(lib: Any, album: Any) -> ReorganizeMove | None:
    items = list(album.items())
    moving = [i for i in items if _item_moves(lib, i)]
    if not moving:
        return None
    from_path = _commonpath_of_dirs([i.path for i in items])
    to_path = _commonpath_of_dirs([i.destination(basedir=lib.directory) for i in items])
    return ReorganizeMove(
        kind="album",
        label=album_label(album),
        from_path=from_path,
        to_path=to_path,
        track_count=len(moving),
    )


def _describe_singleton(lib: Any, item: Any) -> ReorganizeMove | None:
    if not _item_moves(lib, item):
        return None
    from_path = os.path.dirname(os.fsdecode(item.path))
    to_path = os.path.dirname(os.fsdecode(item.destination(basedir=lib.directory)))
    return ReorganizeMove(
        kind="singleton",
        label=singleton_label(item),
        from_path=from_path,
        to_path=to_path,
        track_count=1,
    )


def _scope_label(
    *, scope: ReorganizeScope, artist: str | None, album_id: int | None, albums: list[Any]
) -> str:
    if scope == "album":
        return album_label(albums[0]) if albums else f"album {album_id}"
    if scope == "artist":
        return artist or "Unknown"
    return "library"


def _orphan_preview(
    lib: Any,
    *,
    scope: ReorganizeScope,
    trash_dir: Path | None,
    ignore_dirs: tuple[Path, ...],
) -> tuple[list[OrphanFolder], int]:
    """Read-only orphan candidates for the preview (no move). Empty when no trash_dir
    is configured, and empty for a non-library scope: a scoped run's husks only form
    DURING the run (post-move), so they are surfaced in the result count, not the
    pre-move preview. Library scope lists the pre-existing backlog."""
    if trash_dir is None or scope != "library":
        return [], 0
    music_dir = Path(os.fsdecode(lib.directory))
    folders = find_orphan_folders(
        music_dir, seeds=None, trash_dir=trash_dir, ignore_dirs=ignore_dirs
    )
    rows: list[OrphanFolder] = []
    for f in folders[:PREVIEW_ROW_CAP]:
        try:
            rel = str(f.relative_to(music_dir))
        except ValueError:
            rel = str(f)
        file_count = sum(1 for _d, _s, files in os.walk(f) for _f in files)
        rows.append(OrphanFolder(name=f.name, path=rel, file_count=file_count))
    return rows, len(folders)


def plan_reorganize(
    lib: Any,
    *,
    scope: ReorganizeScope,
    artist: str | None = None,
    album_id: int | None = None,
    trash_dir: Path | None = None,
    ignore_dirs: tuple[Path, ...] = (),
) -> ReorganizePlan:
    """Read-only dry run: what would move under the current path config + (when a
    trash_dir is given) the audio-empty husks that would be moved to Trash."""
    with lib.music_dir_context():
        albums = _albums_for_scope(lib, scope=scope, artist=artist, album_id=album_id)
        singletons = _singletons_for_scope(lib, scope=scope)
        total = len(albums) + len(singletons)
        all_moves: list[ReorganizeMove] = []
        for album in albums:
            m = _describe_album(lib, album)
            if m is not None:
                all_moves.append(m)
        for item in singletons:
            m = _describe_singleton(lib, item)
            if m is not None:
                all_moves.append(m)
        will_move = len(all_moves)
        moves = all_moves[:PREVIEW_ROW_CAP]
        orphans, orphans_total = _orphan_preview(
            lib, scope=scope, trash_dir=trash_dir, ignore_dirs=ignore_dirs
        )
        return ReorganizePlan(
            scope=scope,
            scope_label=_scope_label(scope=scope, artist=artist, album_id=album_id, albums=albums),
            total=total,
            will_move=will_move,
            already_in_place=total - will_move,
            moves=moves,
            truncated=will_move > len(moves),
            orphans=orphans,
            orphans_total=orphans_total,
        )


def reorganize_album(lib: Any, album: Any) -> ReorganizeOutcome:
    """Move one album to match the current path config. Never raises.

    Skips empty albums and already-organized albums. ``Album.move`` relocates all
    items + art, prunes the vacated dirs, and updates DB paths (store=True).
    """
    label = album_label(album)
    pending: list[tuple[int, bytes, bytes]] = []  # bound before try: the except path reads it
    with lib.music_dir_context():
        try:
            items = list(album.items())
            if not items or not any(_item_moves(lib, i) for i in items):
                return ReorganizeOutcome(status="skipped", label=label)
            source_dir = _commonpath_of_dirs([i.path for i in items])  # before the move
            pending = [
                (int(i.id), bytes(i.path), bytes(i.destination(basedir=lib.directory)))
                for i in items
                if _item_moves(lib, i)
            ]
            with lib.transaction():
                album.move(MoveOperation.MOVE, store=True)
            # Album.move re-fetches its own item objects; the local `items` list is
            # NOT mutated, so re-read the album's items to see the post-move paths.
            after = {int(i.id): bytes(i.path) for i in album.items()}
            _carry_sidecars(lib, pending, after)
            problems = _verify_moves(pending, after)
            if problems:
                return ReorganizeOutcome(status="failed", label=label, error="; ".join(problems))
            return ReorganizeOutcome(status="moved", label=label, source_dir=source_dir or None)
        # FilesystemError (permission/disk-full/NAS I/O from beets' util.move/copy)
        # subclasses HumanReadableError(Exception), NOT OSError — catch it too, or
        # one bad album escapes this "never raises" adapter and aborts the whole sweep.
        except (ValueError, OSError, FilesystemError) as exc:
            # Album.move moves+stores ONE ITEM AT A TIME, and beets' Transaction
            # commits even while an exception propagates (dbcore/db.py __exit__ has
            # no rollback branch). Items moved before a mid-album crash are
            # therefore permanently re-pathed — they never re-enter `pending` on a
            # later run, so this is the ONLY chance to carry their sidecars before
            # the vacated folder decays into a husk the orphan sweep trashes.
            # Suppress everything: this adapter never raises, and no carry problem
            # may displace the original failure being reported in `exc`.
            if pending:
                with contextlib.suppress(Exception):
                    _carry_sidecars(lib, pending, {int(i.id): bytes(i.path) for i in album.items()})
            return ReorganizeOutcome(
                status="failed", label=label, error=str(exc) or exc.__class__.__name__
            )


def reorganize_singleton(lib: Any, item: Any) -> ReorganizeOutcome:
    """Move one singleton (album_id is None) to match the config. Never raises."""
    label = singleton_label(item)
    pending: list[tuple[int, bytes, bytes]] = []  # bound before try: the except path reads it
    with lib.music_dir_context():
        try:
            if not _item_moves(lib, item):
                return ReorganizeOutcome(status="skipped", label=label)
            source_dir = os.path.dirname(os.fsdecode(item.path))  # before the move
            old_path = bytes(item.path)
            dest = bytes(item.destination(basedir=lib.directory))
            pending = [(int(item.id), old_path, dest)]
            with lib.transaction():
                item.move(MoveOperation.MOVE, with_album=False, store=True)
            # item.move mutates this same object's `.path`, so compare it directly.
            after = {int(item.id): bytes(item.path)}
            _carry_sidecars(lib, pending, after)
            problems = _verify_moves(pending, after)
            if problems:
                return ReorganizeOutcome(status="failed", label=label, error="; ".join(problems))
            return ReorganizeOutcome(status="moved", label=label, source_dir=source_dir or None)
        except (ValueError, OSError, FilesystemError) as exc:  # see reorganize_album
            # Same crash-carry as reorganize_album. A failed single-file move
            # usually leaves item.path unchanged, making this a no-op — but a
            # partially-applied move (art moved, then store raised) is cheap to
            # cover with the identical best-effort pass.
            if pending:
                with contextlib.suppress(Exception):
                    _carry_sidecars(lib, pending, {int(item.id): bytes(item.path)})
            return ReorganizeOutcome(
                status="failed", label=label, error=str(exc) or exc.__class__.__name__
            )
