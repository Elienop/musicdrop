"""Trash management: list / restore / empty.

Sits above the low-level relocation primitive (``app.beets.trash``). Restore has
two shapes, and which one a row gets is decided by the origin record the mover
left inside it (``app.beets.trash_record``):

* **move back** — the folder came from a known place inside the library, so it
  is moved straight back there and re-imported IN PLACE. Exact, and the only
  exit from Trash an audio-free art/booklet husk has ever had.
* **re-import as-is** — no usable origin (a row trashed before origins were
  recorded, or one whose files were relocated individually out of a shared
  folder). Unchanged from what Trash has always done: an as-is directive import
  in move mode, where beets files the album under the CURRENT path templates and
  its duplicate detection makes the attempt safe (a matching library album →
  SKIP, files stay in Trash).

beets imports allowed here (inside app/beets/, CLAUDE.md rule 3).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from beets.library import Item, Library

from app.beets.import_session import ImportBridge, WebImportSession, run_import_worker
from app.beets.library import (
    _coerce_int,
    _coerce_optional_str,
    _coerce_str,
    _music_dir,
    require_library_present,
)
from app.beets.trash_record import delete_trash_origin, move_back_target, read_trash_origin
from app.fsutil import exists
from app.models.bank import BankApplyDirective
from app.models.import_models import AlbumOutcomeStatus
from app.models.trash import EmptyResult, RestoreResult, TrashedAlbum, TrashRestoreMode
from app.wire import display_path, resolve_display_path

logger = logging.getLogger(__name__)


class TrashRestoreIncompleteError(Exception):
    """A move-back restore left the folder neither in Trash nor in the library.

    Raised only when the recovery itself could not be completed — the forward
    move failed part-way (a cross-filesystem ``shutil.move`` copies then removes,
    so it has a partial state a same-filesystem rename does not), or the folder
    had to be put back in Trash after a failed import and that move failed too.
    Carries both paths, because the person reading it is the one who has to look
    at them.
    """


#: Shown for a row that predates the origin record. Decision 2 of the owner's
#: ruling: such a row must say why it cannot be put back, not quietly restore
#: somewhere else.
_NO_RECORD_NOTE = (
    "MusicDrop has no record of where this came from — it was moved to Trash before"
    " origins were recorded. Restoring re-imports it, so beets files it under your"
    " current naming rules rather than putting it back."
)
#: ``moved="items"``: the album's files were taken out of a folder it shared.
_SHARED_FOLDER_NOTE = (
    "This album's files were moved out of a folder it shared with other music, so"
    " MusicDrop cannot put them back exactly. Restoring re-imports the album under your"
    " current naming rules."
)
#: A recorded origin that is no longer inside the library — normally because the
#: library's ``directory`` now points somewhere else.
_OUTSIDE_LIBRARY_NOTE = (
    "The folder this came from is not inside the current music library, so MusicDrop"
    " will not move it back there. Restoring re-imports it under your current naming"
    " rules."
)


def list_trashed_albums(trash_dir: Path, *, music_dir: str) -> list[TrashedAlbum]:
    """Group the audio files under ``trash_dir`` into trashed albums (by tags).

    Reads each file's tags via ``Item.from_path`` (no DB), groups by
    ``(albumartist, album)``, and keys each group on the common parent dir
    relative to ``trash_dir`` (handles whole-folder, per-item, and multi-disc
    layouts). A missing dir yields ``[]``.

    ``music_dir`` is the library's music root, and it is REQUIRED rather than
    defaulted: it is what decides whether each row's recorded origin is still
    inside the library, and a default would silently answer that question with
    "no check ran" for any caller that forgot it. One extra JSON read per
    top-level entry, next to the tag read this already does per FILE.
    """
    if not trash_dir.exists():
        return []
    groups = _walk_trash_groups(trash_dir)
    albums = _albums_from_groups(groups, trash_dir, music_dir=music_dir)
    albums.extend(_audio_free_entries(trash_dir, groups, music_dir=music_dir))
    albums.sort(key=lambda a: ((a.album_artist or "").lower(), (a.album or "").lower()))
    return albums


def _walk_trash_groups(trash_dir: Path) -> dict[str, list[Any]]:
    """Collect audio files under ``trash_dir`` grouped by their top-level entry.

    Reads each file's tags via ``Item.from_path`` (no DB) and keys each item on
    the entry directly under ``trash_dir`` (``top`` is the restore/empty key).
    """
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
    return groups


def _albums_from_groups(
    groups: dict[str, list[Any]], trash_dir: Path, *, music_dir: str
) -> list[TrashedAlbum]:
    """Turn each tag group into a :class:`TrashedAlbum`, keyed on the raw name."""
    albums: list[TrashedAlbum] = []
    for folder, items in groups.items():
        first = items[0]
        mode, note, origin = _restore_fields(trash_dir / folder, music_dir=music_dir)
        albums.append(
            TrashedAlbum(
                # Grouping stays keyed on the RAW name; only the emitted key is
                # made display-safe, and ``resolve_trash_child`` maps it back.
                folder=display_path(folder),
                album_artist=_coerce_str(first.albumartist) or None,
                album=_coerce_str(first.album) or None,
                year=_coerce_int(getattr(first, "year", 0)) or None,
                track_count=len(items),
                format=_coerce_optional_str(getattr(first, "format", None)),
                restore_mode=mode,
                restore_note=note,
                origin=origin,
            )
        )
    return albums


def _restore_fields(
    entry: Path, *, music_dir: str
) -> tuple[TrashRestoreMode, str | None, str | None]:
    """``(restore_mode, restore_note, origin)`` for one top-level Trash entry.

    The three ways a row loses its move-back each get their OWN sentence rather
    than one generic "cannot restore": the user's next action differs (wait for
    nothing / put it back by hand / re-point the library), and a row that simply
    predates the record must say so — that is the owner's decision 2.
    """
    record = read_trash_origin(entry)
    if record is None:
        return "import", _NO_RECORD_NOTE, None
    origin = display_path(record.origin)
    if record.moved != "folder":
        return "import", _SHARED_FOLDER_NOTE, origin
    if move_back_target(record, music_dir=music_dir) is None:
        return "import", _OUTSIDE_LIBRARY_NOTE, origin
    return "move_back", None, origin


def _audio_free_entries(
    trash_dir: Path, groups: dict[str, list[Any]], *, music_dir: str
) -> list[TrashedAlbum]:
    """Zero-track entries for top-level trash dirs that produced no audio group."""
    # Audio-free trashed folders (art/sidecar husks the orphan sweep relocates here)
    # carry no Item rows, so the tag-grouping above never lists them. Surface each
    # top-level trash dir that produced no audio group as a zero-track entry —
    # otherwise it is invisible in the Trash UI, has no per-entry Restore/Empty
    # affordance, and Empty-all deletes it silently (the page under-reporting what
    # it destroys). Dirs only; hidden/system names skipped.
    albums: list[TrashedAlbum] = []
    for entry in sorted(trash_dir.iterdir()):
        if entry.is_dir() and not entry.name.startswith(".") and entry.name not in groups:
            mode, note, origin = _restore_fields(entry, music_dir=music_dir)
            albums.append(
                TrashedAlbum(
                    folder=display_path(entry.name),
                    album_artist=None,
                    album=None,
                    year=None,
                    # Zero means "nothing here produced a readable media Item",
                    # NOT "no audio": _walk_trash_groups skips every file
                    # ``Item.from_path`` raises on, while beets' own discovery
                    # takes every non-ignored file in the folder as a candidate
                    # (``albums_in_dir``, importer/tasks.py:1184-1216, no
                    # extension or media filter). So a folder listed at 0 tracks
                    # can still restore. The UI disables the affordance on this
                    # count, which is the honest place for a hint; do NOT
                    # "strengthen" it into a backend refusal — that would make a
                    # restorable folder permanently unrestorable. It is also why
                    # ``restore_mode`` has no "unavailable" value: this count is
                    # not evidence a row cannot restore, and a contract field
                    # would read as if it were.
                    track_count=0,
                    format=None,
                    # An audio-free husk with a record is the row this whole
                    # feature exists for: it can be put back exactly, and Restore
                    # is the ONLY thing standing between it and permanent
                    # deletion. The UI's ``track_count == 0`` disable has to give
                    # way to ``restore_mode`` here.
                    restore_mode=mode,
                    restore_note=note,
                    origin=origin,
                )
            )
    return albums


def restore_album(lib: Library, folder_abs: str, *, trash_dir: Path) -> RestoreResult:
    """Restore a trashed folder, returning the outcome. Synchronous.

    ONE entry point with a branch, not a second endpoint: the caller asks for
    "put this back" and the record decides how much of that is possible. A row
    with no usable origin gets exactly the behaviour it has always had, so the
    fallback is the old function unchanged rather than a degraded new one.
    """
    entry = Path(folder_abs)
    record = read_trash_origin(entry)
    origin = move_back_target(record, music_dir=_music_dir(lib))
    if origin is None:
        result = _restore_by_import(lib, folder_abs, trash_dir=trash_dir, in_place=False)
        if result.restored and record is not None:
            # A record that is no longer about anything: beets has moved the
            # files out from under it. Left in place it outlives its subject —
            # and a ``moved="folder"`` record whose origin was merely outside the
            # library at the time would start offering a move-back again the day
            # the user points ``directory`` back, on a folder that is now empty.
            delete_trash_origin(entry)
        return result
    return _restore_to_origin(lib, entry, origin, trash_dir=trash_dir)


def _restore_by_import(
    lib: Library, folder_abs: str, *, trash_dir: Path, in_place: bool
) -> RestoreResult:
    """Import a folder AS-IS through the directive path, returning the outcome.

    An asis directive session never parks, so this is synchronous. beets'
    duplicate detection + the directive's ``duplicate_action=None`` SKIP a folder
    that duplicates a library album — the safe "restore after adding a
    replacement" case, and the reason a move-back can hand its already-moved
    folder to this and still trust the answer.

    ``in_place`` is what separates the two restores. Move mode (``False``) lets
    beets file the album under the current path templates, which is the honest
    answer when nothing recorded where it belongs. In-place (``True``) is for the
    move-back, where the folder is ALREADY at its recorded origin and beets must
    add it without touching a file.
    """
    bridge = ImportBridge()
    directive = BankApplyDirective(action="asis")
    session = WebImportSession(
        lib, None, [os.fsencode(folder_abs)], None, bridge, trash_dir, directive=directive
    )
    run_import_worker(
        session, move=None if in_place else True, in_place=in_place, directive=directive
    )
    outcomes = bridge.drain_outcomes()
    for outcome in outcomes:
        if outcome.album_id is not None:
            return RestoreResult(restored=True, reason="restored", album_id=outcome.album_id)
    if any(o.status is AlbumOutcomeStatus.needs_dup_resolution for o in outcomes):
        return RestoreResult(restored=False, reason="already_in_library")
    return RestoreResult(restored=False, reason="could_not_restore")


def _restore_to_origin(
    lib: Library, entry: Path, origin: Path, *, trash_dir: Path
) -> RestoreResult:
    """Move ``entry`` back to ``origin`` and re-import it there. All or nothing.

    Three guards, each answering a different way this can be the wrong thing to
    do right now:

    * ``require_library_present`` — the STRONGER root predicate, not the cheap
      one. This writes into the music library, and the state it has to refuse is
      a dropped share whose local mountpoint still holds a stray entry: the cheap
      check passes there, and the restore would move the album onto a phantom
      directory that disappears the moment the share comes back. Its one residual
      is an EMPTY library, which has no album to sample and so passes on the root
      check alone.
    * the origin already exists — refuse rather than merge or divert. A restore
      that lands beside the thing it was meant to be is not a restore, and
      ``shutil.move`` onto an existing directory moves the folder INSIDE it.
    * the import did not land the album — put the folder back in Trash and report
      the import's own answer, so a duplicate reads exactly as it does today.

    The parent is created because beets prunes an empty artist folder on the way
    out; that is the normal case, not an anomaly.
    """
    require_library_present(lib)
    if exists(origin):
        return RestoreResult(restored=False, reason="origin_occupied")
    try:
        origin.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry), str(origin))
    except OSError as exc:
        # No undo attempted: a same-filesystem rename either happened or did
        # not, and a cross-filesystem one that failed part-way has left a
        # partial copy whose relationship to the source only a human can judge.
        # Naming both paths beats guessing.
        raise TrashRestoreIncompleteError(
            f"could not move {display_path(entry)} back to {display_path(origin)}: {exc}."
            " Check both paths before retrying."
        ) from exc
    try:
        result = _restore_by_import(lib, str(origin), trash_dir=trash_dir, in_place=True)
    except Exception:
        # The import failed outright. Undo the move so the caller's error is
        # about a folder still safely in Trash — and if the undo ALSO fails, say
        # so here rather than replacing the original exception with it.
        try:
            _return_to_trash(origin, entry)
        except TrashRestoreIncompleteError:
            logger.exception(
                "could not return %s to Trash after a failed restore", display_path(origin)
            )
        raise
    if not result.restored and result.reason == "could_not_restore" and not _holds_media(origin):
        # The art/booklet husk the orphan sweep relocates: there was never a
        # library row to recreate, so the move IS the restore and an empty import
        # is beets agreeing there was nothing to import — it builds an album task
        # only once at least one file reads as an ``Item``
        # (``importer/tasks.py:1084-1089``). Classified AFTER the import rather
        # than skipping it on the same probe up front, so a file beets can read
        # and this cannot (it runs ``fix_extension`` first, so an extensionless
        # media file is beets' to find) still gets its album back.
        result = RestoreResult(restored=True, reason="restored")
    if not result.restored:
        _return_to_trash(origin, entry)
        return result
    # Only now: the record has ridden along inside the folder and its job is
    # done. The accepted cost of a sidecar over a central manifest.
    delete_trash_origin(origin)
    return result


def _holds_media(folder: Path) -> bool:
    """Whether any file under ``folder`` reads as a beets ``Item``.

    The same probe the listing's track count uses, asked of a restored folder to
    tell "there was nothing to import" apart from "the import failed". Stops at
    the first hit.
    """
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                Item.from_path(os.fsencode(os.path.join(root, name)))
            except Exception:  # non-media file (e.g. cover art): keep looking
                continue
            return True
    return False


def _return_to_trash(origin: Path, entry: Path) -> None:
    """Undo a move-back whose import did not land. Raises if it cannot.

    Refuses to move onto an existing ``entry``: ``shutil.move`` would put the
    folder INSIDE it and bury the album one level down under its own name.
    """
    if exists(entry) or not exists(origin):
        raise TrashRestoreIncompleteError(
            f"cannot return {display_path(origin)} to Trash at {display_path(entry)}:"
            " the source is gone or the Trash entry is occupied. Check both paths."
        )
    try:
        shutil.move(str(origin), str(entry))
    except OSError as exc:
        raise TrashRestoreIncompleteError(
            f"the restore did not land and {display_path(origin)} could not be returned to"
            f" Trash at {display_path(entry)}: {exc}. Check both paths."
        ) from exc
    # Only removes the artist folder if the restore is what created it.
    with contextlib.suppress(OSError):
        origin.parent.rmdir()


def resolve_trash_child(trash_dir: Path, rel: str) -> Path:
    """Resolve ``trash_dir/rel`` and refuse anything outside it.

    These paths are ``rm -rf`` / import targets, so reject traversal (``../``),
    the Trash root itself, and a non-existent child by raising ``ValueError``.

    ``rel`` is the ``folder`` the listing emitted, which is display-safe — so a
    folder whose real name is not valid UTF-8 comes back carrying placeholders.
    ``resolve_display_path`` maps that onto the real entry (and raises
    ``AmbiguousDisplayName`` rather than guess when two folders display alike),
    keeping such an album restorable instead of stranding it in Trash.
    """
    base = trash_dir.resolve()
    dest = resolve_display_path(trash_dir, rel).resolve()
    if dest == base or not dest.is_relative_to(base) or not exists(dest):
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
