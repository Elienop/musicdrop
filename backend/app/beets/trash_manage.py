"""Trash management: list / restore (as-is re-import) / empty.

Sits above the low-level relocation primitive (``app.beets.trash``). Restore
re-imports a trashed folder AS-IS via the directive path — beets' duplicate
detection makes it safe (a matching library album → SKIP, files stay in Trash).

beets imports allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from beets.library import Item, Library

from app.beets.import_session import ImportBridge, WebImportSession, run_import_worker
from app.beets.library import _coerce_int, _coerce_optional_str, _coerce_str
from app.models.bank import BankApplyDirective
from app.models.import_models import AlbumOutcomeStatus
from app.models.trash import EmptyResult, RestoreResult, TrashedAlbum


def list_trashed_albums(trash_dir: Path) -> list[TrashedAlbum]:
    """Group the audio files under ``trash_dir`` into trashed albums (by tags).

    Reads each file's tags via ``Item.from_path`` (no DB), groups by
    ``(albumartist, album)``, and keys each group on the common parent dir
    relative to ``trash_dir`` (handles whole-folder, per-item, and multi-disc
    layouts). A missing dir yields ``[]``.
    """
    if not trash_dir.exists():
        return []
    # Group by the entry directly under trash_dir — each trashed album is its own
    # subdir there — NOT by tags, so same-tagged or untagged sibling folders stay
    # distinct and reachable (grouping by tags collapsed them onto folder=".",
    # which the restore/empty guard then 404s). Multi-disc folders group naturally
    # (one shared top dir); ``top`` is the restore/empty key.
    groups: dict[str, list[Any]] = {}
    for root, _dirs, files in os.walk(trash_dir):
        for name in files:
            path = os.path.join(root, name)
            try:
                item = Item.from_path(os.fsencode(path))
            except Exception:  # non-media file (e.g. cover art): skip
                continue
            top = os.path.relpath(path, trash_dir).split(os.sep, 1)[0]
            groups.setdefault(top, []).append(item)
    albums: list[TrashedAlbum] = []
    for folder, items in groups.items():
        first = items[0]
        albums.append(
            TrashedAlbum(
                folder=folder,
                album_artist=_coerce_str(first.albumartist) or None,
                album=_coerce_str(first.album) or None,
                year=_coerce_int(getattr(first, "year", 0)) or None,
                track_count=len(items),
                format=_coerce_optional_str(getattr(first, "format", None)),
            )
        )
    albums.sort(key=lambda a: ((a.album_artist or "").lower(), (a.album or "").lower()))
    return albums


def restore_album(lib: Library, folder_abs: str, *, trash_dir: Path) -> RestoreResult:
    """Re-import a trashed folder AS-IS (move-mode), returning the outcome.

    Synchronous: an asis directive session never parks. beets' duplicate
    detection + the directive's ``duplicate_action=None`` SKIP a folder that
    duplicates a library album (files stay in Trash) — the safe "restore after
    adding a replacement" case.
    """
    bridge = ImportBridge()
    directive = BankApplyDirective(action="asis")
    session = WebImportSession(
        lib, None, [os.fsencode(folder_abs)], None, bridge, trash_dir, directive=directive
    )
    run_import_worker(session, move=True, directive=directive)
    outcomes = bridge.drain_outcomes()
    for outcome in outcomes:
        if outcome.album_id is not None:
            return RestoreResult(restored=True, reason="restored", album_id=outcome.album_id)
    if any(o.status is AlbumOutcomeStatus.needs_dup_resolution for o in outcomes):
        return RestoreResult(restored=False, reason="already_in_library")
    return RestoreResult(restored=False, reason="could_not_restore")


def resolve_trash_child(trash_dir: Path, rel: str) -> Path:
    """Resolve ``trash_dir/rel`` and refuse anything outside it.

    These paths are ``rm -rf`` / import targets, so reject traversal (``../``),
    the Trash root itself, and a non-existent child by raising ``ValueError``.
    """
    base = trash_dir.resolve()
    dest = (trash_dir / rel).resolve()
    if dest == base or not dest.is_relative_to(base) or not dest.exists():
        raise ValueError(f"{rel!r} is not a trashed album")
    return dest


def empty_one(folder_abs: str) -> EmptyResult:
    """Permanently remove one trashed entry — a folder or a loose file."""
    path = Path(folder_abs)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return EmptyResult(removed=1)


def empty_all(trash_dir: Path) -> EmptyResult:
    """Permanently remove everything under ``trash_dir``."""
    if not trash_dir.exists():
        return EmptyResult(removed=0)
    removed = 0
    for child in trash_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    return EmptyResult(removed=removed)
