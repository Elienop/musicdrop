"""Trash management: list / restore / empty.

Sits above the low-level relocation primitive (``app.beets.trash``). Restore has
two shapes, and which one a row gets is decided by the origin the mover recorded
for it in the sibling store (``app.beets.trash_origins`` — one JSON file per
Trash entry, keyed on the entry's name, outside the trashed folder entirely):

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
import errno
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
from app.beets.trash_origins import delete_trash_origin, move_back_target, read_trash_origin
from app.fsutil import exists
from app.models.bank import BankApplyDirective
from app.models.import_models import AlbumOutcomeStatus
from app.models.trash import EmptyResult, RestoreResult, TrashedAlbum, TrashRestoreMode
from app.wire import display_path, resolve_display_path

logger = logging.getLogger(__name__)


class TrashEmptyPartialError(Exception):
    """Empty removed some entries and could not remove others.

    Like :class:`TrashRestoreIncompleteError`, the message is USER-facing — the
    API puts it straight into the 500's ``detail`` and the Trash page renders
    that — so it is written for someone standing in front of their own files.

    Raised at the END of the sweep rather than at the first failure, which is
    the whole point: aborting mid-loop left the user with no idea whether one
    entry had been removed or eleven, and the count was lost with the exception.
    Nothing is at risk either way — every entry is either gone or still in
    Trash — so the honest answer is to finish the ones that can be finished and
    then say exactly which could not.
    """


class TrashRestoreIncompleteError(Exception):
    """A move-back restore left the folder neither in Trash nor in the library.

    Raised only when the recovery itself could not be completed — the forward
    move failed part-way (a cross-filesystem move copies then removes, so it has
    a partial state a same-filesystem rename does not; see :func:`_move_no_merge`
    for which of the two runs), or the folder had to be put back in Trash after a
    failed import and that move failed too.
    Carries both paths, because the person reading it is the one who has to look
    at them — and the person reading it is the USER, not an operator: the API
    puts this message straight into the 500's ``detail`` and the Trash page
    renders that in an alert. Write the sentence for someone standing in front
    of their own files, and say which path to look at.
    """


#: Shown for a row with no record we can use. Decision 2 of the owner's ruling:
#: such a row must say why it cannot be put back, not quietly restore somewhere
#: else.
#:
#: It names TWO causes, and the second one is why: ``read_trash_origin``
#: collapses "no file" and "a file we cannot trust" to the same ``None``, so this
#: sentence is also what a row gets when the record write FAILED thirty seconds
#: ago on a full or read-only volume. Blaming that on "trashed before origins
#: were recorded" would send the user looking at a folder's age instead of at the
#: disk, and every later delete would lose its origin the same way, unnoticed.
#: The log line is the tie-breaker, so the note points at it — ``read_trash_origin``
#: WARNs for the unusable case and stays silent for the absent one.
_NO_RECORD_NOTE = (
    "MusicDrop has no usable record of where this came from — either it was moved to"
    " Trash before origins were recorded, or writing that record failed (the server log"
    " says which). Restoring re-imports it, so beets files it under your current naming"
    " rules rather than putting it back."
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


def list_trashed_albums(
    trash_dir: Path, *, origins_dir: Path, music_dir: str
) -> list[TrashedAlbum]:
    """Group the audio files under ``trash_dir`` into trashed albums (by tags).

    Reads each file's tags via ``Item.from_path`` (no DB), groups by
    ``(albumartist, album)``, and keys each group on the common parent dir
    relative to ``trash_dir`` (handles whole-folder, per-item, and multi-disc
    layouts). A missing dir yields ``[]``.

    ``origins_dir`` and ``music_dir`` are both REQUIRED rather than defaulted,
    for the same reason: between them they decide all three of ``restore_mode``,
    ``restore_note`` and ``origin``, and a caller that forgot either would
    silently degrade EVERY row to "import" while every unit test below still
    passed. One extra JSON read per top-level entry, next to the tag read this
    already does per FILE.

    Nothing is REAPED here. An origin file whose entry was deleted outside
    MusicDrop is litter, and the tempting sweep — "unlink every record with no
    matching entry" — cannot tell an empty Trash dir from a Trash dir on a share
    that just dropped, which is the state where it would destroy every remaining
    origin at once. The hazard orphans actually pose is closed in the name
    allocator instead (``trash._unique_trash_dest``).
    """
    if not trash_dir.exists():
        return []
    groups = _walk_trash_groups(trash_dir)
    albums = _albums_from_groups(groups, trash_dir, origins_dir=origins_dir, music_dir=music_dir)
    albums.extend(
        _audio_free_entries(trash_dir, groups, origins_dir=origins_dir, music_dir=music_dir)
    )
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
    groups: dict[str, list[Any]], trash_dir: Path, *, origins_dir: Path, music_dir: str
) -> list[TrashedAlbum]:
    """Turn each tag group into a :class:`TrashedAlbum`, keyed on the raw name."""
    albums: list[TrashedAlbum] = []
    for folder, items in groups.items():
        first = items[0]
        mode, note, origin = _restore_fields(
            trash_dir / folder, origins_dir=origins_dir, music_dir=music_dir
        )
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
    entry: Path, *, origins_dir: Path, music_dir: str
) -> tuple[TrashRestoreMode, str | None, str | None]:
    """``(restore_mode, restore_note, origin)`` for one top-level Trash entry.

    The three ways a row loses its move-back each get their OWN sentence rather
    than one generic "cannot restore": the user's next action differs (wait for
    nothing / put it back by hand / re-point the library), and a row that simply
    predates the record must say so — that is the owner's decision 2.

    ``entry.name`` is the store's key, and it must be the RAW on-disk name — the
    same string ``_walk_trash_groups`` groups on and ``resolve_trash_child`` maps
    back to. Passing the display form (``display_path``) would look right and
    read nothing for any folder whose name is not valid UTF-8.
    """
    record = read_trash_origin(origins_dir, entry.name)
    if record is None:
        return "import", _NO_RECORD_NOTE, None
    origin = display_path(record.origin)
    if record.moved != "folder":
        return "import", _SHARED_FOLDER_NOTE, origin
    if move_back_target(record, music_dir=music_dir) is None:
        return "import", _OUTSIDE_LIBRARY_NOTE, origin
    return "move_back", None, origin


def _audio_free_entries(
    trash_dir: Path, groups: dict[str, list[Any]], *, origins_dir: Path, music_dir: str
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
            mode, note, origin = _restore_fields(
                entry, origins_dir=origins_dir, music_dir=music_dir
            )
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
                    # can still restore. The UI shows a "may still work" hint on
                    # this count and deliberately does NOT disable Restore
                    # (``SettingsTrashPage.tsx``); do NOT "strengthen" it into a
                    # backend refusal or a disabled control — that would make a
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


def restore_album(
    lib: Library, folder_abs: str, *, trash_dir: Path, origins_dir: Path
) -> RestoreResult:
    """Restore a trashed folder, returning the outcome. Synchronous.

    ONE entry point with a branch, not a second endpoint: the caller asks for
    "put this back" and the record decides how much of that is possible. A row
    with no usable origin gets exactly the behaviour it has always had, so the
    fallback is the old function unchanged rather than a degraded new one.
    """
    entry = Path(folder_abs)
    record = read_trash_origin(origins_dir, entry.name)
    origin = move_back_target(record, music_dir=_music_dir(lib))
    if origin is None:
        result = _restore_by_import(
            lib, folder_abs, trash_dir=trash_dir, origins_dir=origins_dir, in_place=False
        )
        if result.restored:
            # A record that is no longer about anything: beets has moved the
            # files out from under it. Left in place it outlives its subject —
            # and because the key is the entry NAME, a later folder taking that
            # name would inherit it. Unconditional, deliberately: an UNREADABLE
            # record also reaches here (``read_trash_origin`` collapses it to
            # ``None``) and it is exactly the file that must not be left to be
            # adopted. In the sidecar design it rode out inside the folder and
            # was inert either way; on the /data side it survives forever.
            delete_trash_origin(origins_dir, entry.name)
        return result
    return _restore_to_origin(lib, entry, origin, trash_dir=trash_dir, origins_dir=origins_dir)


def _restore_by_import(
    lib: Library, folder_abs: str, *, trash_dir: Path, origins_dir: Path, in_place: bool
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
        lib,
        None,
        [os.fsencode(folder_abs)],
        None,
        bridge,
        trash_dir,
        trash_origins_dir=origins_dir,
        directive=directive,
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
    lib: Library, entry: Path, origin: Path, *, trash_dir: Path, origins_dir: Path
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
      ``shutil.move`` onto an existing directory moves the folder INSIDE it. The
      ``exists`` call below answers that cheaply, but it is a PRE-FILTER and not
      the guard: it and the move are two syscalls, so the promise is kept by
      :func:`_move_no_merge`, which cannot be raced. Both answer
      ``origin_occupied``, so the window is invisible to the caller.
    * the import did not land the album — put the folder back in Trash and report
      the import's own answer, so a duplicate reads exactly as it does today. If
      that return ALSO fails, the folder is in neither place and the error says
      so (see the handler); it is the only path here that does not end with the
      files back in Trash.

    The parent is created because beets prunes an empty artist folder on the way
    out; that is the normal case, not an anomaly.

    One residual in the occupancy answer, stated rather than hidden: ``exists``
    follows symlinks, so a DANGLING link at the origin (``music/X -> /gone``)
    reads as absent, the move is attempted, and ``os.rename`` answers ENOTDIR —
    which :func:`_move_no_merge` normalises to the same ``origin_occupied`` the
    user is told about a path ``ls`` shows as broken. The FILES are safe (still
    in Trash, nothing moved) and the answer is right for the wrong-looking
    reason, so this is a wording problem and not a data one; ``origin_occupied``
    is a contract value the UI renders, so widening it is a contract change.
    """
    require_library_present(lib)
    # The store's key, named ONCE so the two clean-ups below cannot drift apart.
    # Both run at a point where the entry is no longer in Trash, and the obvious
    # thing to reach for there is the folder actually in front of you —
    # ``origin.name``, which is what the sidecar version effectively used. That
    # is a DIFFERENT string whenever the two differ (a collision suffix, a husk
    # the sweep renamed) and belongs to no Trash entry at all. Not load-bearing
    # as a capture: ``Path.name`` is a string and does not follow the file.
    entry_name = entry.name
    if exists(origin):
        return RestoreResult(restored=False, reason="origin_occupied")
    try:
        origin.parent.mkdir(parents=True, exist_ok=True)
        try:
            _move_no_merge(entry, origin)
        except FileExistsError:
            # The origin appeared between the check above and the move — a
            # sync client, an *arr or the user. Same answer as the pre-filter's,
            # and the folder is still sitting untouched in Trash.
            return RestoreResult(restored=False, reason="origin_occupied")
    except OSError as exc:
        # No undo attempted: a same-filesystem rename either happened or did
        # not, and a cross-filesystem one that failed part-way has left a
        # partial copy whose relationship to the source only a human can judge.
        # Naming both paths beats guessing — and saying they may BOTH hold the
        # folder now beats "check both paths", which reads as "one of them".
        # ``_move_no_merge``'s EXDEV branch copies before it removes, so a
        # failure mid-copy leaves a partial copy at the origin with the Trash
        # entry intact, and a failure mid-``rmtree`` leaves a complete copy at
        # the origin with a partial entry still in Trash.
        raise TrashRestoreIncompleteError(
            f"could not move {display_path(entry)!r} back to {display_path(origin)!r}:"
            f" {exc}. Across filesystems this copies before it removes, so the folder may"
            f" now be in BOTH places — look at both before retrying. Retrying cannot make"
            f" it worse: a restore refuses while anything is at the destination."
        ) from exc
    try:
        result = _restore_by_import(
            lib, str(origin), trash_dir=trash_dir, origins_dir=origins_dir, in_place=True
        )
    except Exception as exc:
        # The import failed outright. Undo the move so the caller's error is
        # about a folder still safely in Trash.
        try:
            _return_to_trash(origin, entry)
        except TrashRestoreIncompleteError as undo:
            # ``%r``, not ``%s``, and the same in the message this logs the
            # traceback of. A Trash folder's name comes from the album's own
            # tags, and ``_trash_container_name`` neutralises only path
            # separators — so a newline or an ANSI escape in an ``albumartist``
            # survives into the folder name, and ``display_path`` replaces only
            # UNDECODABLE bytes, never control characters. Interpolated raw,
            # that forges log lines. ``repr`` escapes them and leaves ordinary
            # text (including the U+FFFD placeholder) readable.
            logger.exception(
                "could not return %r to Trash after a failed restore", display_path(origin)
            )
            # The entry is no longer in Trash — it is stranded at ``origin`` and
            # the name is free again — so the record describes nothing and would
            # be inherited by whatever takes that name next. (The allocator
            # refuses to reuse a recorded name, so leaving it would burn the name
            # rather than mis-steer a restore; deleting it is still the honest
            # state.) It costs only the contrived recovery of hand-moving the
            # folder BACK to Trash, which is the one thing the message below does
            # not ask for. Never raises, so it cannot make this path worse.
            #
            # (Under the old sidecar this deletion had a second, larger job: the
            # record rode out inside the folder, and the re-import this error
            # asks for then left a husk behind because beets refuses to prune a
            # source dir that still holds a file. A sibling store cannot cause
            # that, which is one of the reasons it replaced the sidecar.)
            delete_trash_origin(origins_dir, entry_name)
            # The propagating error is the UNDO's story, not the import's. The
            # import's exception alone answers "Restore failed: <beets error>",
            # which sends the user to look in Trash — where there is now
            # nothing. This is the one state the folder is in neither place the
            # user can act on, so the sentence has to name both paths and say
            # which one to look at. Chained on the import error, so its
            # traceback is still reachable; the undo's is in the log above.
            raise TrashRestoreIncompleteError(
                f"the restore could not be completed and could not be undone."
                f" {display_path(entry)!r} is no longer in Trash: it was moved to"
                f" {display_path(origin)!r} and was NOT added to the library database."
                f" Check that path — importing the folder there is what finishes putting"
                f" the album back. The import failed with: {exc}. Returning it to Trash"
                f" then failed with: {undo}."
            ) from exc
        raise
    if not result.restored and result.reason == "could_not_restore" and not _holds_media(origin):
        # The art/booklet husk the orphan sweep relocates: there was never a
        # library row to recreate, so the move IS the restore and an empty import
        # is beets agreeing there was nothing to import — it builds an album task
        # only once at least one file reads as an ``Item``
        # (``importer/tasks.py:1084-1089``).
        #
        # Classified AFTER the import rather than skipping it on the same probe
        # up front, because beets' discovery is the AUTHORITY on "was there an
        # album here" and :func:`_holds_media` is a heuristic that does not
        # replicate it: beets applies ``ignore``/``ignore_hidden``, extracts
        # archives, and remuxes before reading (``importer/tasks.py:1141-1168``).
        # Asking the probe only once beets has already answered "nothing landed"
        # makes it a tie-breaker on a decided question instead of a gate that
        # could decide it alone, and the import costs nothing on a folder with
        # no media. (An earlier version of this comment justified the ordering
        # with "a file beets can read and this cannot — it runs ``fix_extension``
        # first, so an extensionless media file is beets' to find". That was
        # FALSE: ``fix_extension`` only ADDS an extension where there is none
        # (``beets/util/extension.py:83-95``) and ``Item.from_path`` sniffs by
        # content — measured, an extensionless FLAC reads fine. The decision was
        # right; the reason was not.)
        #
        # ``result.reason == "could_not_restore"`` is a stated precondition, not
        # a live branch: ``already_in_library`` needs an outcome from an album
        # task, an album task needs at least one ``Item``, and this arm runs only
        # where there is none. No test can kill it; it is kept so the arm does
        # not silently depend on that beets-internal fact.
        result = RestoreResult(restored=True, reason="restored")
    if not result.restored:
        # The entry is back in Trash under its own name, so its record is still
        # TRUE and must survive. Deleting it here would strand a returned row on
        # the import-restore fallback for good.
        _return_to_trash(origin, entry)
        return result
    # Only now: the folder is in the library and the Trash entry is gone, so the
    # record has nothing left to describe and its name is free for reuse.
    delete_trash_origin(origins_dir, entry_name)
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


#: ``rename``'s ways of saying "something is already at the destination". POSIX
#: lets an implementation answer a non-empty destination directory with either
#: EEXIST or ENOTEMPTY (Linux picks ENOTEMPTY), and a destination that is a FILE
#: while the source is a directory answers ENOTDIR. All three mean the same thing
#: here, and none of them moved anything.
_DEST_OCCUPIED = frozenset({errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR})


def _move_no_merge(src: Path, dest: Path) -> None:
    """Move ``src`` onto ``dest``, refusing rather than moving INSIDE it.

    ``shutil.move`` treats an existing DIRECTORY destination as a container: it
    puts the source in there under its own name. Both callers check ``exists``
    first, but a check and a move are two syscalls, so anything that creates the
    destination in the window between them — a sync client, an ``*arr``, the
    user — turns a documented refusal into a silent burial one level down.
    Measured: the album ends up at ``<origin>/<trash entry name>/``, still
    present, still complete, and in a place nothing looks for it.

    ``rename`` closes the window because the kernel makes the check and the move
    one operation, so it goes first and only a cross-filesystem move falls back
    to a copy. The refusal is normalised to ``FileExistsError`` whatever errno
    the kernel chose, so a caller can tell "the destination was taken" apart from
    a move that half-happened.

    Two residuals, stated rather than hidden:

    * ``rename`` REPLACES an EMPTY directory at the destination instead of
      refusing it. Nothing is buried or merged when it does, which is the
      property the callers need; an empty dir is also what a pruning beets or a
      half-finished sync leaves behind, so refusing it would be worse.
    * Both callers move a DIRECTORY, and the EXDEV branch is written for that. A
      file source raises ``NotADirectoryError`` here instead of quietly taking
      ``shutil.move``'s file path, which would bury it the same way.
    """
    try:
        os.rename(src, dest)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            # Different filesystems, so no rename can do it and the move has to
            # copy. ``copytree``'s own ``os.makedirs(..., exist_ok=False)`` is
            # the atomic refusal ``shutil.move`` skips: one ``mkdir`` syscall
            # that raises ``FileExistsError`` rather than descending into a
            # destination that appeared. ``symlinks=True`` + ``copy2`` are what
            # ``shutil.move`` itself uses for a directory, so the copy is
            # unchanged — only the container behaviour is dropped.
            shutil.copytree(src, dest, symlinks=True)
            shutil.rmtree(src)
            return
        if exc.errno in _DEST_OCCUPIED:
            raise FileExistsError(exc.errno, os.strerror(exc.errno), str(dest)) from exc
        raise


def _return_to_trash(origin: Path, entry: Path) -> None:
    """Undo a move-back whose import did not land. Raises if it cannot.

    Refuses to move onto an existing ``entry``: ``shutil.move`` would put the
    folder INSIDE it and bury the album one level down under its own name. The
    ``exists`` check is the cheap pre-filter; :func:`_move_no_merge` is what
    makes the refusal hold when something creates ``entry`` in the window.

    Only ONE half of that pre-filter is a guard. ``exists(entry)`` is: nothing
    below it would otherwise refuse an occupied entry in time, and the burial it
    stops is silent. ``not exists(origin)`` is a MESSAGE — ``_move_no_merge`` on
    a missing source raises ``FileNotFoundError``, which is an ``OSError`` the
    arm below already turns into this same exception type, so removing it
    changes only the wording the operator reads. Both are kept, and saying which
    is which beats letting the next reader treat them as one guard.
    """
    if exists(entry) or not exists(origin):
        raise TrashRestoreIncompleteError(
            f"cannot return {display_path(origin)!r} to Trash at {display_path(entry)!r}:"
            " the source is gone or the Trash entry is occupied. Check both paths."
        )
    try:
        _move_no_merge(origin, entry)
    except OSError as exc:
        raise TrashRestoreIncompleteError(
            f"the restore did not land and {display_path(origin)!r} could not be returned"
            f" to Trash at {display_path(entry)!r}: {exc}. Check both paths."
        ) from exc
    # Removes the parent when it is EMPTY, which is broader than "only if the
    # restore created it" — a pre-existing artist folder the failed restore has
    # left empty goes too. That is deliberate and matches what beets' own source
    # pruning does: an empty artist folder is not state the library wants back,
    # and ``rmdir`` cannot touch one that still holds another album.
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


def empty_one(folder_abs: str, *, origins_dir: Path) -> EmptyResult:
    """Permanently remove one trashed entry — a folder or a loose file.

    The origin record goes with it, and strictly AFTER: a failed ``rmtree``
    raises out of here, and losing the record for an entry that is still sitting
    in Trash would silently downgrade its row to an import-restore. With the
    sidecar this ordering was free (the ``rmtree`` took the record with it);
    keyed on the name in a sibling dir, it is a rule.
    """
    path = Path(folder_abs)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    delete_trash_origin(origins_dir, path.name)
    return EmptyResult(removed=1)


def empty_all(trash_dir: Path, *, origins_dir: Path) -> EmptyResult:
    """Permanently remove everything under ``trash_dir``.

    ``is_dir()`` FOLLOWS symlinks and ``shutil.rmtree`` refuses one, so a
    symlinked entry used to raise ``OSError`` here and wedge the whole
    operation: nothing after it in ``iterdir`` order was removed, and every
    retry failed identically, leaving Trash impossible to empty through the app.
    ``empty_one`` cannot clear it either -- ``resolve_trash_child`` resolves the
    child and 404s anything landing outside Trash, which is a guard worth
    keeping -- so the entry was unremovable by any route.

    An entry gets there without anything hostile: ``_album_root`` is
    ``dirname(item.path)``, so an album whose own folder is a symlink into
    another volume is trashed as a symlink, because ``shutil.move`` preserves
    them. Treating it as a leaf is also the only safe reading of "remove
    everything under ``trash_dir``" -- following it would ``rm -rf`` a directory
    that merely happens to be pointed at.

    Each entry's origin record is dropped INSIDE the loop, right after that entry
    is removed, so a fault part-way through leaves a consistent pair rather than
    a set of records for entries that are still there. Deliberately per-child and
    not "wipe the origins dir at the end": a ``trash_dir`` whose share has
    dropped presents as an empty directory, and emptying it would then destroy
    the origins of every entry that is still on the real volume. The records left
    behind by an entry deleted outside MusicDrop stay as litter — see
    ``trash._unique_trash_dest`` for why that is harmless.
    """
    if not trash_dir.exists():
        return EmptyResult(removed=0)
    removed = 0
    failed: list[str] = []
    first: OSError | None = None
    for child in trash_dir.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as exc:
            # Carry on. One entry the app cannot remove -- a root-owned file, a
            # permission bit, a share that dropped half way -- used to abort the
            # whole sweep and take the count with it, so the user was told
            # nothing and could not tell 1-of-12 from 11-of-12. The entry stays
            # in Trash either way; only the reporting was ever at stake.
            failed.append(display_path(child.name))
            first = first or exc
            continue
        delete_trash_origin(origins_dir, child.name)
        removed += 1
    if failed:
        # Named, not just counted: the user's next move is to look at them, and
        # a bare number does not say which. Capped because Trash can be large
        # and this lands in an HTTP body a browser renders.
        shown = ", ".join(repr(n) for n in failed[:5])
        more = f" and {len(failed) - 5} more" if len(failed) > 5 else ""
        raise TrashEmptyPartialError(
            f"removed {removed} of {removed + len(failed)}."
            f" {len(failed)} could not be removed and are still in Trash: {shown}{more}."
            f" The first failure was: {first}"
        )
    return EmptyResult(removed=removed)
