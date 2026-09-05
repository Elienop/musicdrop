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
import logging
import os
import shutil
import stat
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
from app.beets.protected import (
    ProtectedTreeError,
    ProtectedTrees,
    open_checked_dir,
    protected_match,
    refuse_protected_tree,
)
from app.beets.trash_origins import (
    clear_trash_origins,
    delete_trash_origin,
    move_back_target,
    read_trash_origin,
)
from app.fsutil import exists, move_no_merge, occupied
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
    """A move-back restore could not be completed, and the files may have moved.

    Raised when the file work itself failed: the destination folder could not be
    created, the forward move failed (part-way, across filesystems, where a copy
    precedes the remove — see :func:`move_no_merge`), or the folder had to be
    put back in Trash after a failed import and that move failed too. NOT raised
    for an import that merely declined (a duplicate); that is a ``RestoreResult``.

    Every one of those sentences answers the same question, and it is answered
    from the DISK rather than from which arm raised — :func:`_whereabouts` looks
    at both paths after the failure and says where the folder is now, whether it
    reached the library database, and what to do next. The person reading it is
    the USER, not an operator: the API puts this message straight into the 500's
    ``detail`` and the Trash page renders that in an alert. Write the sentence
    for someone standing in front of their own files.
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
#:
#: What the WARN says is not only "the write failed". ``read_trash_origin`` logs a
#: reason per rejection, and more than one of them is a record that IS on disk:
#: "it is the record for a different Trash entry" (the file at this entry's key
#: belongs to a longer-named entry whose key was truncated onto the same name),
#: "it could not be read" (measured with the store at mode 000), "it is not an
#: object". The sentence's third arm is deliberately the WIDE one — "that record
#: may be unusable now" — so every one of those lands inside it instead of
#: outside a two-cause disjunction, and the log line stays the tie-breaker.
_NO_RECORD_NOTE = (
    "MusicDrop has no usable record of where this came from: it may predate origin"
    " records, its record may have failed to write, or that record may be unusable now"
    " (the server log says which). Restoring re-imports it, so beets files it under your"
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
#: The Trash entry is itself a SYMLINK. Ordinary rather than hostile:
#: ``trash._album_root`` is ``dirname(item.path)``, so an album whose own folder
#: is a symlink into another volume is trashed AS a symlink (``shutil.move``
#: recreates the link and unlinks the original), which means only the LINK was
#: ever moved — the album's files never left the volume they were on.
#:
#: Such a row used to fall through to :data:`_NO_RECORD_NOTE`, and all three of
#: that sentence's claims are false here: ``trash._record_origin`` declines
#: DELIBERATELY (so nothing failed and the server log says nothing), the row is
#: not old, and "Restoring re-imports it" is not on offer at all —
#: :func:`resolve_trash_child` refuses a child that is a link
#: (:func:`_is_symlinked_entry`, the same predicate this row's mode comes from),
#: so Restore answers 404. Say what will really happen instead, and say where the
#: files are: they are the one thing here that was never at risk.
#:
#: The last sentence is about the row's OTHER button, and it used to be wrong in
#: the user's favour: "Emptying this entry removes only the link" is true of
#: ``DELETE /api/trash/all`` (:func:`empty_all` unlinks a symlinked child) and
#: false of the Empty beside this note, which goes through the SAME
#: :func:`resolve_trash_child` refusal as Restore and answers 404 with the link
#: still there. Both per-row actions are named, because the note has to be true
#: of every affordance rendered next to it.
#:
#: "elsewhere", not "on another volume": another volume is the ORDINARY
#: provenance described above, not a property of every entry that gets this
#: note. A link pointing at a SIBLING Trash entry (``<trash>/Alias ->
#: ./RealAlbum``), at a file, or at nothing that is there right now (a dangling
#: one, which is what an unmounted volume looks like) reads the same way and is
#: refused the same way, and for each of those the old clause was simply wrong.
#: What is left is what holds for all of them: it is a link, nothing follows it,
#: and the files are wherever it points.
#:
#: "a folder or file", for the same reason the volume went. The rows MusicDrop
#: itself creates are links to a FOLDER (``_album_root`` is a directory), but a
#: hand-placed top-level link to a media FILE reaches the listing too: ``os.walk``
#: lists it among ``files`` and ``Item.from_path`` follows it, so the row arrives
#: with real tags and this note. Measured 2026-09-02 by listing a Trash holding
#: both shapes: ``folder='linked.flac' mode=refused tracks=1 fmt='FLAC'`` and
#: ``folder='Linked Folder' mode=refused tracks=0 fmt=None``, this string verbatim
#: on both. A noun that is wrong for one of them is a note contradicting the row
#: it sits on.
#:
#: ``frontend/src/pages/settings/SettingsTrashPage.test.tsx`` keeps its own COPY
#: of this string as a fixture (``REFUSED_NOTE``). It does not read this one, so
#: it does not fail when this changes — it goes stale silently. Update it with
#: any edit here.
_SYMLINKED_ENTRY_NOTE = (
    "This Trash entry is a link to a folder or file elsewhere, so MusicDrop will not"
    " restore it — following the link would import files that were never in Trash. The"
    " album's own files were never moved: they are still where the link points, and"
    " adding that folder through Import is what puts the album back in the library."
    " Restore and this row's own Empty both refuse it; Empty all removes the link, and"
    " only the link."
)


def _is_symlinked_entry(entry: Path) -> bool:
    """Whether a Trash entry is itself a LINK — the refusal both sides make.

    ONE predicate, two callers, and that is the point rather than tidiness:
    :func:`_restore_fields` renders ``"refused"`` from it and
    :func:`resolve_trash_child` turns the two per-row routes down with it, so the
    row's promise and the routes' behaviour cannot answer differently. They used
    to: the listing asked ``os.path.islink`` while the routes resolved the child
    and tested containment. Those agree for a link pointing OUT of Trash and
    disagree for one pointing at a SIBLING entry, where the resolved path is
    inside the Trash dir — measured before they were one predicate, on
    ``<trash>/Alias -> ./RealAlbum``: ``DELETE /api/trash?folder=Alias``
    answered ``200 {"removed": 1}``, ``rmtree``'d ``RealAlbum`` (a different
    row) and left ``Alias`` in place. Folder names arrive from ``/music``,
    which this deployment's threat
    model treats as attacker-writable.

    LEXICAL, and asked of the named path itself: what the link points at is never
    consulted, so a dangling link, a link to another volume and a link to a
    sibling Trash entry are one case with one answer, decided before anything is
    resolved.

    ``os.path.islink`` rather than ``Path.is_symlink`` because both callers take
    a name a client or the filesystem chose: ``Path.is_symlink`` absorbs only
    ENOENT/ENOTDIR/EBADF/ELOOP, so an overlong component still raises
    ENAMETOOLONG out of it (the reason ``app/fsutil.py`` exists), while
    ``os.path.islink`` answers False — the right answer on both sides, since what
    cannot be stat'ed is neither a link to honour nor a trashed album. That is no
    longer an unkillable preference: with this on the route's path,
    ``tests/test_trash_manage.py::test_resolve_trash_child_refuses_an_overlong_name``
    fails with the other spelling (measured: OSError out of a resolver whose
    contract is ``ValueError`` -> 404). It is not the spelling every caller wants
    — ``trash._record_origin`` asks about a path ``shutil.move`` has just
    created, where no such name can exist.
    """
    return os.path.islink(entry)


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
    origin at once. The hazard orphans pose is NARROWED elsewhere instead:
    ``trash._unique_trash_dest`` will not hand out a name whose record is still
    on disk. That covers the names MusicDrop hands out and nothing else — a
    folder arriving in Trash by another route (a hand copy, a restored backup, a
    sync client) asks the allocator nothing and can adopt a leftover record.
    ``trash_origins`` states that as an open residual.

    A record left over that way is reached in one place, and it is not here:
    :func:`empty_all` calls ``trash_origins.clear_trash_origins`` (its only
    caller, grepped) once it has removed at least one entry AND found
    ``trash_dir`` empty afterwards. That gate does not have the ambiguity above:
    having removed an entry is what says the directory walked was the real one,
    so the empty Trash it then reads is one it emptied itself rather than a
    share that dropped. The listing can make no such claim — it
    removes nothing — which is why the reasoning above still holds here.
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

    The FOUR ways a row loses its move-back each get their OWN sentence rather
    than one generic "cannot restore": the user's next action differs (wait for
    nothing / put it back by hand / re-point the library / go to the volume the
    link points at), and a row that simply predates the record must say so —
    that is the owner's decision 2. Three of the four are still an import; the
    symlinked one is ``"refused"``, because it is the only one whose per-row
    routes both answer 404 before any work starts.

    ``entry.name`` is the store's key, and it must be the RAW on-disk name — the
    same string ``_walk_trash_groups`` groups on and ``resolve_trash_child`` maps
    back to. Passing the display form (``display_path``) would look right and
    read nothing for any folder whose name is not valid UTF-8.
    """
    # The SHARED predicate, not a second spelling of it: whatever makes this row
    # say "refused" is what makes the two per-row routes refuse it
    # (:func:`_is_symlinked_entry`, which also documents why it is
    # ``os.path.islink``). The two used to be written separately and disagreed on
    # a link pointing at a sibling entry.
    #
    # Asked BEFORE the record, and it decides alone. A symlinked entry cannot be
    # restored by any route whatever a record says about it, so a record that
    # somehow exists for this name (written for a DIFFERENT entry that held the
    # name earlier — a leftover the allocator declines to hand back out, though
    # it cannot stop a folder arriving by another route from adopting one; see
    # ``trash_origins``) must not out-vote it.
    if _is_symlinked_entry(entry):
        return "refused", _SYMLINKED_ENTRY_NOTE, None
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
    """Zero-track rows for top-level entries that produced no audio group.

    A directory or a symlink; hidden/system names skipped.

    Loose FILES are left to ``_walk_trash_groups``, which lists any it can read
    as an ``Item`` (``test_empty_one_removes_a_loose_file`` is such a row). One
    it CANNOT read — a stray sidecar dropped straight into the Trash dir — is
    listed by neither, and stays as invisible as the dangling link below used to
    be. Stated as a residual rather than closed here: what the app's own trashing
    moves is an album FOLDER (``trash._album_root``) or the container it makes
    for a shared one, so a loose unreadable file arrives by some other route.
    """
    # Audio-free trashed folders (art/sidecar husks the orphan sweep relocates here)
    # carry no Item rows, so the tag-grouping above never lists them. Surface each
    # top-level trash dir that produced no audio group as a zero-track entry —
    # otherwise it is invisible in the Trash UI, has no per-entry Restore/Empty
    # affordance, and Empty-all deletes it silently (the page under-reporting what
    # it destroys).
    #
    # ``os.path.islink`` is asked BESIDE ``is_dir``, not left to it: ``is_dir``
    # FOLLOWS the link, so a DANGLING one answered False and produced no row at
    # all — and a dangling link is exactly the state ``_SYMLINKED_ENTRY_NOTE``
    # describes, the volume it points at not being mounted. That entry was then
    # invisible: nothing to click, and only ``DELETE /api/trash/all`` removed it
    # (silently, and only if the user emptied everything). It lists as
    # ``"refused"`` like any other link, from the same predicate the two per-row
    # routes turn it down with.
    albums: list[TrashedAlbum] = []
    for entry in sorted(trash_dir.iterdir()):
        if (
            (os.path.islink(entry) or entry.is_dir())
            and not entry.name.startswith(".")
            and entry.name not in groups
        ):
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
                    # this count gets no ``restore_mode`` value of its own: it is
                    # not evidence a row cannot restore, and a contract field
                    # would read as if it were. ``"refused"`` exists for the one
                    # case that is not a guess — a symlinked entry, whose refusal
                    # comes from the guard both per-row routes run.
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
    lib: Library,
    folder_abs: str,
    *,
    trash_dir: Path,
    origins_dir: Path,
    protected: ProtectedTrees,
) -> RestoreResult:
    """Restore a trashed folder, returning the outcome. Synchronous.

    ONE entry point with a branch, not a second endpoint: the caller asks for
    "put this back" and the record decides how much of that is possible. A row
    with no usable origin gets exactly the behaviour it has always had, so the
    fallback is the old function unchanged rather than a degraded new one.

    The unmounted-share guard sits HERE, above the branch, because BOTH arms
    write into the music library and an unmounted share is the same catastrophe
    for either. It used to sit inside the move-back arm only, which meant a row
    with no record — every row trashed before origins existed, and every row
    whose record write failed — answered a dropped share with ``200 restored``
    while beets filed the album onto the bare mountpoint and emptied the Trash
    entry. The share then remounts OVER it: the files are visible nowhere and
    the library holds a row whose files "vanished". Measured on an empty
    mountpoint before this moved: the no-record row returned
    ``restored=True`` and left one file at ``<music>/__/00.flac``, while the
    byte-identical move-back row returned 503 and moved nothing.

    That asymmetry also made the route lie: ``api/trash.py`` declares 503 "the
    music library folder is unavailable, so the folder was not moved out of
    Trash", and README says the same. One guard at the entry point is what
    makes that sentence true for the endpoint rather than for one of its arms.

    ``protected`` is asked here for the same reason: restore is a MOVER, and
    both arms relocate the whole entry out of Trash. On a Trash that is the host
    parent of a bind-mounted music library, a stale record named ``music`` sent
    the move-back through ``move_no_merge``'s copy branch and deleted the
    library while reporting ``restored=True``; the import arm relocates an
    aliased app store's files into the library instead.
    """
    entry = Path(folder_abs)
    require_library_present(lib)
    refuse_protected_tree(entry, protected, action="moved")
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
            # name would inherit it. Called unconditionally, deliberately: an
            # UNREADABLE record also reaches here (``read_trash_origin``
            # collapses it to ``None``) and it is exactly the file that must not
            # be left to be adopted. In the sidecar design it rode out inside the
            # folder and was inert either way; on the /data side it survives
            # forever.
            #
            # Unconditional HERE is not unconditional on disk: ``None`` also
            # covers the one case where the file at this entry's key belongs to
            # a DIFFERENT entry (two long names can share one record file), and
            # ``delete_trash_origin`` keeps that one — it reads the payload's own
            # ``name`` before unlinking. Its docstring owns that exception; this
            # call site deliberately does not repeat the test.
            delete_trash_origin(origins_dir, entry.name)
        return result
    return _restore_to_origin(
        lib, entry, origin, trash_dir=trash_dir, origins_dir=origins_dir, protected=protected
    )


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
    lib: Library,
    entry: Path,
    origin: Path,
    *,
    trash_dir: Path,
    origins_dir: Path,
    protected: ProtectedTrees,
) -> RestoreResult:
    """Move ``entry`` back to ``origin`` and re-import it there. All or nothing.

    Three guards, each answering a different way this can be the wrong thing to
    do right now:

    * ``require_library_present`` — the STRONGER root predicate, not the cheap
      one. This writes into the music library, and the state it has to refuse is
      a dropped share whose local mountpoint still holds a stray entry: the cheap
      check passes there, and the restore would move the album onto a phantom
      directory that disappears the moment the share comes back. It passes an
      EMPTY library, which has no file to sample and so answers on the root check
      alone; and since each slot is now the FILE rather than its folder, it also
      refuses wherever ALL of its sampled albums are missing their sampled file:
      certainly in a single-album library whose track was removed by hand while
      its folder stayed (measured 20 of 20 draws), and at the ``f**K`` rate below
      that (measured 33 refusals in 1000 draws with half of 200 albums in that
      state). Not "one shape" — the figures and the model are in
      ``require_library_present``'s docstring.
    * the origin is OCCUPIED — refuse rather than merge or divert. A restore
      that lands beside the thing it was meant to be is not a restore, and
      ``shutil.move`` onto an existing directory moves the folder INSIDE it. The
      :func:`occupied` call below answers that cheaply, but it is a PRE-FILTER
      and not the guard: it and the move are two syscalls, so the promise is kept
      by :func:`move_no_merge`, which cannot be raced. Both answer
      ``origin_occupied``, so the window is invisible to the caller — and both
      answer an EMPTY DIRECTORY at the origin the same way too (it is removed and
      the restore proceeds; see :func:`occupied` for why that is not a hole).
    * the import did not land the album — put the folder back in Trash and report
      the import's own answer, so a duplicate reads exactly as it does today. If
      that return ALSO fails, :func:`_undo_failure` looks at the disk and says
      where the folder actually ended up. That is one of TWO paths here that can
      end with the files not wholly back in Trash; the other is the forward move
      failing part-way (the ``except OSError`` after :func:`move_no_merge`
      below), where a fault mid-``rmtree`` on the copy branch leaves a complete
      copy at the origin with a partial entry still in Trash. Both answer by
      looking at the disk (:func:`_whereabouts`) rather than by guessing.

    The parent is created because beets prunes an empty artist folder on the way
    out; that is the normal case, not an anomaly. It is created in its OWN try,
    not the move's: a read-only or full music share, or a plain file at a parent
    component, fails here with nothing moved and nothing to look for, and the
    move's sentence ("the folder may now be in BOTH places") would send the user
    hunting for a copy at a path that does not even exist.

    One residual in the occupancy answer, stated rather than hidden: ``exists``
    follows symlinks, so a DANGLING link at the origin (``music/X -> /gone``)
    reads as absent, the move is attempted, and ``os.rename`` answers ENOTDIR —
    which :func:`move_no_merge` normalises to the same ``origin_occupied`` the
    user is told about a path ``ls`` shows as broken. The FILES are safe (still
    in Trash, nothing moved) and the answer is right for the wrong-looking
    reason, so this is a wording problem and not a data one; ``origin_occupied``
    is a contract value the UI renders, so widening it is a contract change.
    """
    # No ``require_library_present`` here: it moved up to ``restore_album``, the
    # only caller, so it covers the import arm too. Re-adding it here would be a
    # second check no test could kill.
    # The store's key, named ONCE so the two clean-ups below cannot drift apart.
    # Both run at a point where the entry is no longer in Trash, and the obvious
    # thing to reach for there is the folder actually in front of you —
    # ``origin.name``, which is what the sidecar version effectively used. That
    # is a DIFFERENT string whenever the two differ (a collision suffix, a husk
    # the sweep renamed) and belongs to no Trash entry at all. Not load-bearing
    # as a capture: ``Path.name`` is a string and does not follow the file.
    entry_name = entry.name
    if occupied(origin):
        return RestoreResult(restored=False, reason="origin_occupied")
    try:
        origin.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # NOTHING has moved yet, so this must not borrow the move's sentence.
        # The causes are all on the music share — read-only, full, a permission
        # bit, or a plain FILE sitting at one of the parent components — and the
        # folder is exactly where it was.
        _, where = _whereabouts(entry, origin)
        raise TrashRestoreIncompleteError(
            f"could not create the folder {display_path(origin.parent)!r} to restore into:"
            f" {exc}. The move was not attempted — the music library may be read-only or"
            f" full, or a file may be sitting at one of those names. {where}"
        ) from exc
    try:
        move_no_merge(entry, origin)
    except FileExistsError:
        # The origin appeared between the check above and the move — a
        # sync client, an *arr or the user. Same answer as the pre-filter's,
        # and the folder is still sitting untouched in Trash.
        return RestoreResult(restored=False, reason="origin_occupied")
    except OSError as exc:
        # No undo attempted: a same-filesystem rename either happened or did
        # not, and a cross-filesystem one that failed part-way has left a
        # partial copy whose relationship to the source only a human can judge.
        # So the sentence does not GUESS from which arm raised — it looks at the
        # disk (:func:`_whereabouts`) and names where the folder is now. That
        # matters most on the EXDEV branch, which copies before it removes: a
        # failure mid-copy leaves a partial copy at the origin with the Trash
        # entry intact, and a failure mid-``rmtree`` leaves a complete copy at
        # the origin with a partial entry still in Trash.
        _, where = _whereabouts(entry, origin)
        raise TrashRestoreIncompleteError(
            f"could not move the folder back out of Trash: {exc}. {where} Retrying cannot"
            f" make it worse: a restore refuses while anything is at the destination."
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
            # The propagating error is the UNDO's story, not the import's. The
            # import's exception alone answers "Restore failed: <beets error>",
            # which sends the user to look in Trash — where there may now be
            # nothing. Chained on the import error, so its traceback is still
            # reachable; the undo's is in the log ``_undo_failure`` writes.
            raise _undo_failure(
                entry,
                origin,
                origins_dir=origins_dir,
                entry_name=entry_name,
                what_failed=(
                    f"The import failed with: {_one_full_stop(str(exc))} Returning it to"
                    f" Trash then failed with: {undo}"
                ),
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
        try:
            _return_to_trash(origin, entry)
        except TrashRestoreIncompleteError as undo:
            # The SAME double failure as the import-raised arm above, reached the
            # other way: beets answered "not restored" (a duplicate, or nothing
            # landed) instead of raising, and the undo then hit the same retaken
            # Trash entry. This arm used to call ``_return_to_trash`` bare, so
            # the user got its one-line "check both paths" — no word that the
            # album is absent from the library database, nothing about where the
            # files actually are, and no next step — while the twin arm composed
            # all three. One shared step now, so the two cannot drift again.
            raise _undo_failure(
                entry,
                origin,
                origins_dir=origins_dir,
                entry_name=entry_name,
                what_failed=(
                    f"The import did not add the album ({result.reason}) and returning it"
                    f" to Trash then failed with: {undo}"
                ),
            ) from undo
        return result
    # Only now: the folder is in the library and the Trash entry is gone, so the
    # record has nothing left to describe and its name is free for reuse.
    delete_trash_origin(origins_dir, entry_name)
    return result


def _whereabouts(entry: Path, origin: Path) -> tuple[bool, str]:
    """``(the folder is still in Trash, a sentence saying where it is NOW)``.

    Every failure inside a move-back has to answer the same question — *where
    are my files?* — and the honest answer comes from the DISK, not from which
    arm raised. The arms cannot know: ``move_no_merge``'s EXDEV branch (the only
    one the shipped Docker layout ever takes, ``/data`` and ``/music`` being
    separate mounts) copies before it removes, so the same exception covers
    "nothing was copied", "half a copy at the origin", and "a whole copy at the
    origin with a half-emptied entry still in Trash". A sentence chosen from the
    arm is right for one of those and wrong for the other two.

    The two booleans are read ONCE and both consumers use that one reading, which
    is why this returns the flag rather than letting the caller re-``exists`` it:
    a second look could disagree with the sentence just composed, and the flag
    decides whether the origin record is destroyed (:func:`_undo_failure`).

    Every sentence names BOTH paths, each anchored to its own phrase ("in Trash
    at X", "at the origin Y"), because the two are interchangeable-looking
    absolute paths and the user acts on them: told them the wrong way round, they
    would import a stranger's folder and leave the album where nothing looks for
    it. Every sentence also says whether the album reached the library database —
    that is the difference between "your files are safe, retry" and "your files
    are safe but the album is gone from the library until you import them".
    """
    in_trash = exists(entry)
    at_origin = exists(origin)
    if in_trash and at_origin:
        # Two causes reach this state and nothing on disk tells them apart, so
        # the sentence names both rather than asserting the likelier one: the
        # cross-filesystem move copies before it removes, and something outside
        # MusicDrop can retake the Trash name while the album sits at the origin.
        return in_trash, (
            f"There is something at BOTH places now: in Trash at {display_path(entry)!r},"
            f" and at the origin {display_path(origin)!r}. One of them may be an"
            f" incomplete copy — a move across filesystems copies before it removes — or"
            f" something outside MusicDrop may have taken the Trash name. Compare them"
            f" before removing either. It was NOT added to the library database."
        )
    if at_origin:
        return in_trash, (
            f"The folder is no longer in Trash at {display_path(entry)!r}: it is at the"
            f" origin {display_path(origin)!r}, and it was NOT added to the library"
            f" database. Importing that folder is what finishes putting the album back."
        )
    if in_trash:
        return in_trash, (
            f"The folder is still in Trash at {display_path(entry)!r} and nothing is at"
            f" the origin {display_path(origin)!r}. It was NOT added to the library"
            f" database, so nothing has been lost and a retry is safe."
        )
    return in_trash, (
        f"MusicDrop can no longer find the folder at EITHER path: not in Trash at"
        f" {display_path(entry)!r}, and not at the origin {display_path(origin)!r}. It was"
        f" NOT added to the library database. Look at both paths — something outside"
        f" MusicDrop moved or removed it."
    )


def _undo_failure(
    entry: Path,
    origin: Path,
    *,
    origins_dir: Path,
    entry_name: str,
    what_failed: str,
) -> TrashRestoreIncompleteError:
    """Compose the error for a restore that failed AND could not be undone.

    Used by BOTH arms that undo a move-back — the import that raised and the
    import that answered "not restored" — because they leave the user in exactly
    the same place and used to tell them very different things: one composed both
    paths, the database status and a next step, the other let
    :func:`_return_to_trash`'s one-liner propagate.

    It also settles the origin record, from the SAME observation as the sentence:
    the record is dropped only when the Trash entry is no longer there, because
    an entry that is still there — whole, or a half-removed copy — is a row whose
    record is still the truth, and losing it downgrades that row to an
    import-restore forever. When the name IS free the record describes nothing
    and would be inherited by whatever earns that name next. (What holds the
    doubtful case up is the paragraph below, NOT the allocator: the origin is
    occupied by our own stranded album, so ``_restore_to_origin`` refuses the
    exact restore a kept record advertises. ``trash._unique_trash_dest`` only
    declines to HAND OUT a name whose record is still on disk, and the folder
    that retook this Trash entry got there without asking it.)

    The residual, stated rather than hidden: what retook the Trash entry may be a
    STRANGER's folder rather than a piece of ours, and this cannot tell them
    apart. That row then carries a record pointing at the origin our album is
    stranded at, so it advertises an exact restore. That promise is refused only
    while the origin stays OCCUPIED: ``_restore_to_origin`` answers
    ``origin_occupied`` on the spot, and it is the stranded album itself that
    makes it do so. The message above sends the user to compare the two paths and
    remove one, and removing the copy at the ORIGIN takes the refusal with it —
    measured, restoring the stranger's row then moves that folder to our album's
    recorded path and consumes the Trash entry. Nothing is merged or destroyed
    (the origin is empty by then), but a stranger has been filed under a name
    that was never its own.

    Kept anyway, and the trade is the point: deleting the record on the guess
    costs every REAL row whose entry is half-removed its exact restore, for good.
    A wrong promise that usually refuses beats a real record destroyed — "usually"
    being the honest word, which this used to leave out.
    """
    # ``%r``, not ``%s``, and the same in the message this logs the traceback of.
    # A Trash folder's name comes from the album's own tags, and
    # ``_trash_container_name`` neutralises only path separators — so a newline
    # or an ANSI escape in an ``albumartist`` survives into the folder name, and
    # ``display_path`` replaces only UNDECODABLE bytes, never control characters.
    # Interpolated raw, that forges log lines. ``repr`` escapes them and leaves
    # ordinary text (including the U+FFFD placeholder) readable.
    logger.exception("could not return %r to Trash after a failed restore", display_path(origin))
    in_trash, where = _whereabouts(entry, origin)
    if not in_trash:
        # Never raises, so it cannot make this path worse.
        delete_trash_origin(origins_dir, entry_name)
    # ``what_failed`` already ends its own sentence — both call sites finish it
    # with an undo error whose message carries a full stop — so one is added only
    # where it is missing. Appended unconditionally, this rendered ".." in the
    # 500 ``detail`` the Trash page shows the user.
    return TrashRestoreIncompleteError(
        f"the restore could not be completed and could not be undone. {where}"
        f" {_one_full_stop(what_failed)}"
    )


def _one_full_stop(text: str) -> str:
    """``text``, trailing whitespace dropped, ending in exactly one full stop.

    Anywhere a message is composed out of somebody else's words, the words may
    already end their own sentence — and a stop appended on top renders ".." in
    the 500 ``detail`` the Trash page puts in front of the user. Both places that
    do that are here: :func:`_undo_failure`'s tail, whose two call sites end with
    an undo error of ours (always a full stop today), and the import error the
    raise arm quotes, which is beets' or a plugin's and can end however it likes
    — measured, an import that failed with "beets could not read the folder."
    rendered "...read the folder.. Returning it to Trash...".

    The whitespace goes FIRST, and it is the same defect one character along: a
    message ending in a newline is already finished, and testing the last
    character alone put the stop AFTER the newline — a stop standing on its own
    in the middle of the sentence the page renders. An exception carrying one is
    ordinary rather than exotic: a plugin quoting a subprocess's output keeps its
    line ending.

    Only "." counts as an ending. A message finishing "!" or "?" gets a stop
    after it, which reads oddly and has never been seen from these two sources;
    widening the set on that guess would let a message end without one.
    """
    text = text.rstrip()
    return text if text.endswith(".") else f"{text}."


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


def _capped(names: list[str]) -> str:
    """Up to five names, then a count: Trash can be large and this lands in a body."""
    shown = ", ".join(repr(n) for n in names[:5])
    return shown + (f" and {len(names) - 5} more" if len(names) > 5 else "")


def _return_to_trash(origin: Path, entry: Path) -> None:
    """Undo a move-back whose import did not land. Raises if it cannot.

    No identity guard: the id set is fixed per request, so every inode here was
    either checked by the forward guard on ``entry`` or created after the set was
    built. The one input left is another actor renaming a store into ``origin``
    mid-import, and refusing that raised "Nothing was moved." over a folder
    already sitting at its origin.

    Refuses to move onto an existing ``entry``: ``shutil.move`` would put the
    folder INSIDE it and bury the album one level down under its own name. The
    ``exists`` check is the cheap pre-filter; :func:`move_no_merge` is what
    makes the refusal hold when something creates ``entry`` in the window.

    Only ONE half of that pre-filter is a guard. ``exists(entry)`` is: nothing
    below it would otherwise refuse an occupied entry in time, and the burial it
    stops is silent. ``not exists(origin)`` is a MESSAGE — ``move_no_merge`` on
    a missing source raises ``FileNotFoundError``, which is an ``OSError`` the
    arm below already turns into this same exception type, so removing it
    changes only the wording the operator reads. Both are kept, and saying which
    is which beats letting the next reader treat them as one guard.

    Each message names both paths, and each path sits in its own phrase ("at the
    origin X" / "the Trash entry Y") rather than being interchangeable at the two
    ends of one sentence: these strings reach the user through
    :func:`_undo_failure`, and a reader who acts on them the wrong way round
    looks in the wrong place for their album. The two conditions get their own
    sentence for the same reason — "the source is gone or the entry is occupied"
    made the reader check both when the code already knew which.
    """
    if exists(entry):
        raise TrashRestoreIncompleteError(
            f"the folder at the origin {display_path(origin)!r} cannot be moved back into"
            f" Trash: something is at the Trash entry {display_path(entry)!r} again."
        )
    if not exists(origin):
        raise TrashRestoreIncompleteError(
            f"there is nothing at the origin {display_path(origin)!r} to move back into"
            f" Trash at {display_path(entry)!r}."
        )
    try:
        move_no_merge(origin, entry)
    except OSError as exc:
        raise TrashRestoreIncompleteError(
            f"the restore did not land and the folder at the origin"
            f" {display_path(origin)!r} could not be moved back into Trash at"
            f" {display_path(entry)!r}: {exc}."
        ) from exc
    # Removes the parent when it is EMPTY, which is broader than "only if the
    # restore created it" — a pre-existing artist folder the failed restore has
    # left empty goes too. That is deliberate and matches what beets' own source
    # pruning does: an empty artist folder is not state the library wants back,
    # and ``rmdir`` cannot touch one that still holds another album.
    with contextlib.suppress(OSError):
        origin.parent.rmdir()


def _reaches_through_a_link(trash_dir: Path, child: Path) -> bool:
    """Whether walking from ``trash_dir`` down to ``child`` crosses a symlink.

    Every component is asked, not just the leaf: ``rel`` is a request string, so
    it can name a path BELOW a link (``Alias/Disc 1``), which a leaf-only test
    clears and ``resolve`` then follows into the row the link points at.

    A ``child`` the base cannot be stripped from answers False, having walked
    nothing: an absolute ``rel``, which replaces the base entirely. The caller
    refuses that shape itself, in the same ``or``.

    A ``child`` that CLIMBS is NOT that case. ``pathlib`` does not collapse
    ``..``, so ``<trash>/../outside/x`` is lexically under ``trash_dir``
    — measured: ``is_relative_to`` answers True and ``relative_to`` yields
    ``('..', 'outside', 'x')`` — and the walk below really runs over those
    components, refusing only if one of them is a link.
    What ANSWERS the climb is the RESOLVED containment check in
    :func:`resolve_trash_child`, where the ``..`` is finally normalised away,
    and not ``is_relative_to``. "Answers", not "refuses": it refuses a climb that
    lands OUTSIDE Trash, and a climb that lands back INSIDE is accepted, naming
    the same entry its plain spelling names. Measured 2026-09-02 through the
    ``TestClient`` app fixture: ``resolve_trash_child(trash, "../trash/Dummy")``
    and ``resolve_trash_child(trash, "Dummy")`` returned the same
    ``<trash>/Dummy``, and ``DELETE /api/trash?folder=../trash/Dummy`` answered
    ``200 {"removed": 1}`` with that entry gone. That is what ``folder=Dummy``
    does, so the climb reaches nothing the plain spelling could not — it is a
    spelling, not a hole. This must not grow into a second, weaker traversal
    check.
    """
    try:
        parts = child.relative_to(trash_dir).parts
    except ValueError:
        return False
    walked = trash_dir
    for part in parts:
        walked = walked / part
        if _is_symlinked_entry(walked):
            return True
    return False


def resolve_trash_child(trash_dir: Path, rel: str) -> Path:
    """Resolve ``trash_dir/rel`` and refuse anything outside it, or behind a link.

    These paths are ``rm -rf`` / import targets, so reject traversal (``../``),
    the Trash root itself, and a non-existent child by raising ``ValueError``.

    TWO refusals run before anything is resolved, and both are lexical — asked
    of the path as WRITTEN, so neither of THEM consults what a link points at
    (the containment check further down does, and that is the point of having
    all three):

    * the path must be under ``trash_dir`` as written. ``resolve_display_path``
      returns ``trash_dir / rel``, and an absolute ``rel`` replaces the base
      entirely, so ``folder=<abs>/Sneak`` arrives as a path this module never
      handed out. Measured on this branch before this check existed, with
      ``Sneak -> <trash>/RealAlbum``: this resolver returned
      ``<trash>/RealAlbum``, ``DELETE /api/trash?folder=<abs>/Sneak`` answered
      ``200 {"removed": 1}``, and ``RealAlbum`` — a row with its own Restore —
      was gone while ``Sneak`` itself was still a link.
    * no component between ``trash_dir`` and the target may be a link
      (:func:`_reaches_through_a_link`, asking the listing's own
      :func:`_is_symlinked_entry`), so a row rendered ``"refused"`` and a request
      naming that row cannot disagree. Containment alone did not refuse a link
      pointing INTO Trash: it resolved to a real entry, passed, and the caller
      then acted on a DIFFERENT row than the one the request named.

    The resolved containment check below is neither of those and is not made
    redundant by them: ``is_relative_to`` is lexical, so ``trash_dir/../Sibling``
    is "under" ``trash_dir`` until ``resolve`` normalises the ``..`` away.

    ``rel`` is the ``folder`` the listing emitted, which is display-safe — so a
    folder whose real name is not valid UTF-8 comes back carrying placeholders.
    ``resolve_display_path`` maps that onto the real entry (and raises
    ``AmbiguousDisplayName`` rather than guess when two folders display alike),
    keeping such an album restorable instead of stranding it in Trash.
    """
    child = resolve_display_path(trash_dir, rel)
    if not child.is_relative_to(trash_dir) or _reaches_through_a_link(trash_dir, child):
        raise ValueError(f"{rel!r} is not a trashed album")
    base = trash_dir.resolve()
    dest = child.resolve()
    if dest == base or not dest.is_relative_to(base) or not exists(dest):
        raise ValueError(f"{rel!r} is not a trashed album")
    return dest


def empty_one(folder_abs: str, *, origins_dir: Path, protected: ProtectedTrees) -> EmptyResult:
    """Permanently remove one trashed entry — a folder or a loose file.

    Its OWN record goes with it, strictly AFTER: a failed ``rmtree`` raises out
    of here, and losing the record for an entry still in Trash would downgrade
    its row to an import-restore. A record naming a DIFFERENT entry stays — two
    long names can share one truncated key, and ``delete_trash_origin`` reads the
    payload's own ``name`` first.

    Raises :class:`~app.beets.protected.ProtectedTreeError` (503) when the entry
    is or holds one of the app's own directories by inode.
    """
    path = Path(folder_abs)
    refuse_protected_tree(path, protected, action="removed")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    delete_trash_origin(origins_dir, path.name)
    return EmptyResult(removed=1)


def empty_all(trash_dir: Path, *, origins_dir: Path, protected: ProtectedTrees) -> EmptyResult:
    """Permanently remove every unprotected entry under ``trash_dir``.

    The root is opened once through
    :func:`~app.beets.protected.open_checked_dir`, and every name is enumerated,
    guarded, stat'd and removed THROUGH that descriptor. Acting on
    ``trash_dir / name`` reopened the path per entry: a rename plus a symlink
    landing anywhere in the loop — 0.40 ms at 10 entries, 16 ms at 500 —
    redirected the removals, measured deleting ``library.db`` and ``config.yaml``.

    An entry that is or holds one of the app's own directories by inode is left
    where it is and named in a :class:`~app.beets.protected.ProtectedTreeError`
    (503) carrying the failed count, raised AFTER the others are removed.

    A symlinked entry is acted on as the LINK: following it would ``rm -rf`` a
    directory merely pointed at, and ``rmtree`` refuses one, which used to wedge
    every entry after it in ``iterdir`` order.

    Each origin record is dropped inside the loop, right after its entry. The
    whole store is swept only when this call REMOVED something AND Trash is
    empty afterwards: emptiness alone would destroy every record when the share
    has dropped.

    The residual list is the BACKLOG entry for this slice.
    """
    if not trash_dir.exists():
        return EmptyResult(removed=0)
    removed = 0
    failed: list[str] = []
    refused: list[str] = []
    first: OSError | None = None
    fd = open_checked_dir(trash_dir, protected)
    try:
        for name in sorted(entry.name for entry in os.scandir(fd)):
            clause = protected_match(name, protected, dir_fd=fd)
            if clause is not None:
                refused.append(f"{display_path(name)!r} {clause}")
                continue
            try:
                if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    shutil.rmtree(name, dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)
            except OSError as exc:
                # Carry on. One entry the app cannot remove -- a root-owned file, a
                # permission bit, a share that dropped half way -- used to abort the
                # whole sweep and take the count with it, so the user was told
                # nothing and could not tell 1-of-12 from 11-of-12. The entry stays
                # in Trash either way; only the reporting was ever at stake.
                failed.append(display_path(name))
                first = first or exc
                continue
            delete_trash_origin(origins_dir, name)
            removed += 1
    finally:
        os.close(fd)
    if refused:
        shown = "; ".join(refused[:5])
        more = f" and {len(refused) - 5} more" if len(refused) > 5 else ""
        # The failed entries ride along NAMED, the way the partial below names
        # them: this raise outranks it, so a bare count left an entry that could
        # not be removed invisible on every retry.
        stuck = f", {len(failed)} could not be removed ({_capped(failed)})" if failed else ""
        those = "those entries" if len(refused) > 1 else "that entry"
        raise ProtectedTreeError(
            f"Refused: {shown}{more}. Removed {removed}{stuck};"
            f" move {those} out of Trash, then retry."
        )
    if failed:
        # Named, not just counted: the user's next move is to look at them.
        raise TrashEmptyPartialError(
            f"removed {removed} of {removed + len(failed)}."
            f" {len(failed)} could not be removed and are still in Trash: {_capped(failed)}."
            f" The first failure was: {_one_full_stop(str(first))}"
        )
    # Suppressed rather than allowed to escape: everything above has already
    # happened, so a ``trash_dir`` that stopped answering between the loop and
    # this re-read must not turn a completed Empty into a 500. Not sweeping is
    # the same conservative side the dropped-share case takes.
    with contextlib.suppress(OSError):
        if removed and not any(trash_dir.iterdir()):
            clear_trash_origins(origins_dir)
    return EmptyResult(removed=removed)
