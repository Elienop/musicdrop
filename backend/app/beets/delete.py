"""Delete (reversible Trash) for whole albums and whole artists.

Front-door delete: move an album's — or every album of an artist's — ENTIRE
folder to Trash (audio + art + ``.lrc``/``.txt`` sidecars + extras) and drop it
from the library, built on :func:`app.beets.trash.trash_album_folder`.

The async ``_op`` functions mirror the duplicates-resolve op: gated behind the
SAME library-job lock + 409 (Apply / import / lyrics / artist-art / reorganize),
run the blocking move in a threadpool, and bind ``music_dir_context`` so beets'
relative item paths resolve on the worker thread (which doesn't inherit the
``beets.context`` ContextVar — same gap as resolve/Apply).
"""

from __future__ import annotations

import os
from pathlib import Path

from beets.library import Library
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.beets.config_editor import _settings, _swap_lock
from app.beets.library import (
    LibraryHandle,
    LibraryRootUnavailableError,
    _coerce_str,
    _require_id,
    require_library_root,
)
from app.beets.protected import ProtectedTreeError, ProtectedTrees, refuse_protected_tree
from app.beets.store_layout import StoreLayoutError, checked_protected_trees, checked_store_dirs
from app.beets.trash import (
    TrashDeleteIncompleteError,
    TrashMoveIncompleteError,
    TrashRowsNotRemovedError,
    trash_album_folder,
    whole_folder_root,
)
from app.beets.trash_origins import TrashOriginsStoreUnusableError
from app.library_busy import library_job_active
from app.models.delete import DeleteResult

#: The promise the two 503 arms below add to the origin-store refusal, and the
#: ONLY place it may be made. The store's own sentence carries no such clause
#: (``trash_origins._STORE_FIX``): every mover relays that sentence, and the
#: artist fan-out and duplicates' resolve-all reach it having already moved
#: albums into Trash and dropped their rows — measured, a 500 that named one
#: album moved to Trash and then said nothing had been deleted. Here it is a
#: fact and not a hope: both arms sit above the transaction's first mutation,
#: since ``require_usable_store`` is ``trash_album_folder``'s first statement,
#: ahead of every branch of it, so the refusal reaches the first album and no
#: further.
_NOTHING_DELETED = "Nothing has been deleted."

#: The layout refusal's own wording, and it can make the same promise for the
#: same reason: ``_checked_store`` runs before the first mutation of either op,
#: so the delete stops at the check with nothing moved and no row dropped.
_NOTHING_DELETED_LAYOUT = "{} " + _NOTHING_DELETED

#: The recovery line for the one state where the reader must not tidy Trash up
#: before reading the message: the rows would not go AND the folder would not
#: come back, so the files can be in Trash, at the album's own folder, or half
#: at each. Named because TWO arms return it — the bare
#: ``TrashDeleteIncompleteError``, and an artist fan-out that stopped on an
#: album in that state. Tests compare it to a literal, not to this name, or the
#: wording would only be pinned against itself.
_DO_NOT_EMPTY_TRASH = (
    "Do NOT empty the Trash folder before reading the message above: it says"
    " where the files are now, from the disk. Compare both paths first."
)


class AlbumNotFoundError(Exception):
    """A referenced album id is not in the library. Maps to 404."""


class ArtistDeletePartialError(Exception):
    """The artist fan-out mutated some albums and then could not finish.

    Deliberately NOT a :class:`~app.beets.library.LibraryRootUnavailableError`,
    even though that is one thing that causes it: the 503 those map to promises
    that nothing was DROPPED, which stops being true the moment one album has
    been through the primitive — and beets commits on the way out of
    the transaction even while unwinding the exception, so the work already done
    cannot be taken back. Falls to the blanket 500 instead, whose message says
    how far the fan-out got.

    Carries ``moved`` — how many of the artist's albums really had FILES
    relocated — because the 500's recovery line offers to find them in Trash,
    and two of the primitive's branches drop an album's rows having moved
    nothing at all (an album with no item rows, and a ghost whose folder is
    already gone). Counted on the primitive's RETURN, so it is a floor and not a
    census: the album this stopped on is never in it, and it can have files in
    Trash all the same. :func:`_recovery` names the ways this file can point at,
    not the whole list; nothing here distinguishes them from a failure that moved
    nothing, which is why that hint asks rather than tells. The
    ``album.remove``-after-the-move window used to be the worst of them and is
    no longer in the set for the whole-folder path — the primitive puts the
    folder back (``decisions.md`` 28 item 4) — but a move that stops part-way
    and the per-item mover's own row-drop window still are.

    Carries ``cause`` too, and for a narrower reason: one of the causes this can
    wrap — :class:`~app.beets.trash.TrashDeleteIncompleteError`, the album whose
    rows would not go AND whose folder would not come back — has a recovery line
    of its own that must not be replaced by the Trash promise, because the whole
    point of that line is to stop the reader emptying Trash before they have
    read where their files are. Wrapping it hid that (measured: the fan-out
    shipped "Files are recoverable in the Trash folder. Retry." for a state
    whose own line says "Do NOT empty the Trash folder"), so :func:`_recovery`
    reads through to it.
    """

    def __init__(self, message: str, *, moved: int, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.moved = moved
        self.cause = cause


def delete_album(
    lib: Library,
    album_id: int,
    *,
    trash_dir: Path,
    origins_dir: Path,
    protected: ProtectedTrees,
    dropped_item_ids: set[int] | None = None,
) -> DeleteResult:
    """Move one album's whole folder to Trash and drop it. 404 on unknown id.

    Binds ``music_dir_context`` (the threadpool thread doesn't inherit it, so the
    move would otherwise get a relative source path); one transaction so the DB
    drop commits with the file move.

    ``dropped_item_ids``, when given, collects the ids of the items this delete
    removes — read BEFORE the trash call, because that call drops the rows and
    ``album.items()`` would then be empty. The caller owes those items' playlists
    a `.m3u8` re-export (the export lists a file that is now in Trash), and the
    out-set is how it learns which ids to pass. Same accumulate-into-a-caller's-
    collection shape the reorganize sweep uses for ``vacated``.
    """
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        if dropped_item_ids is not None:
            dropped_item_ids.update(_require_id(i.id) for i in album.items())
        with lib.transaction():
            trash_path = trash_album_folder(
                lib, album, trash_dir=trash_dir, origins_dir=origins_dir, protected=protected
            )
    return DeleteResult(trashed_albums=1, trash_path=trash_path)


def delete_artist(
    lib: Library,
    artist_name: str,
    *,
    trash_dir: Path,
    origins_dir: Path,
    protected: ProtectedTrees,
    dropped_item_ids: set[int] | None = None,
) -> DeleteResult:
    """Move EVERY album of ``artist_name`` (matched on albumartist) to Trash.

    Album ids are snapshotted before mutating (trashing drops rows). An artist
    with no albums is a safe no-op (``trashed_albums=0``). One transaction wraps
    all the moves so they COMMIT TOGETHER — one commit point, never a rollback:
    beets' ``Transaction.__exit__`` commits unconditionally, including when it is
    unwinding an exception (``beets/dbcore/db.py:924-941``). So a fault part-way
    through leaves the albums already processed trashed and dropped, and no
    amount of error handling here can undo them.

    That shapes how a fault part-way through is reported, in two tiers:

    * **before the first mutation** — the root check below runs ahead of the
      transaction and the primitive re-checks per album, so a root that is
      already unavailable (or drops before the first move) raises
      ``LibraryRootUnavailableError``; an origin store that cannot be used
      raises ``TrashOriginsStoreUnusableError`` from the primitive's own first
      statement, ahead of every branch of it, so it reaches the first album and
      no further. The op answers either with a 503. Nothing
      has been DROPPED whenever that fires; nothing has moved either, unless the
      share went during one album's own move, which the primitive catches with
      that album's rows kept and part of its folder under the Trash container;
    * **after at least one album** — the SAME error is re-raised as
      :class:`ArtistDeletePartialError`, because the 503's promise is no longer
      true. It reaches the user as the 500, naming how far the fan-out got.

    One arm for every cause, and it quotes the error it caught rather than
    naming one: an unmounted share used to be diagnosed here as fact, and the
    predicate that raises it cannot tell that apart from a library whose FILES
    were removed outside MusicDrop while the share is fine — its own message
    offers both ("Either the music share is not mounted, or those files have
    been removed outside MusicDrop"). Passing the cause through in its own words
    is the only version of this sentence that is true in both states.

    ``dropped_item_ids`` collects the ids this fan-out removes (see
    :func:`delete_album`), filled PER ALBUM inside the loop rather than up front:
    a partial run must report exactly the playlists it really invalidated, and an
    album that raised before its own capture never contributed one.
    """
    with lib.music_dir_context():
        target = artist_name.strip()
        album_ids = [
            _require_id(a.id) for a in lib.albums() if _coerce_str(a.albumartist).strip() == target
        ]
        # Ahead of the transaction, not only inside the primitive: this is what
        # makes the 503's "nothing was dropped" a property of the whole
        # operation rather than a property of whichever album happened to be
        # first. Cheap (one isdir + one scandir entry) against N folder moves.
        require_library_root(lib)
        # NO second pre-check for the origin store here, deliberately, and the
        # asymmetry with the line above is the point. ``require_library_root``
        # earns its place because the primitive only re-checks the ROOT inside
        # one branch, so without it a fan-out could reach its second album
        # before anything refused. ``require_usable_store`` needs no such help:
        # it is ``trash_album_folder``'s FIRST statement — checked structurally,
        # first non-docstring node of the body — ahead of every branch of it, so
        # the first album already refuses with nothing dropped. A copy here
        # changes no outcome any test can see: adding it back left the whole
        # suite green (measured). That is not an argument that the
        # delete tests would have caught one if it did, and the difference has
        # been measured too — ``origin_recorded``'s refusing arm survives
        # tests/test_delete.py + tests/test_trash.py and is killed
        # only in tests/test_trash_origins_store.py, so this file's own pins are
        # not where every delete-path guard lives. It would also refuse a
        # fan-out over an artist with NO albums, which mutates nothing at all.
        # Two counters, because they answer different questions and a run can
        # have one without the other. ``mutated`` is albums whose ROWS are gone,
        # which is what makes the 503's "nothing was dropped" false and so
        # decides which tier a fault gets. ``moved`` is albums whose FILES are
        # in Trash, which is the only thing the message and the recovery hint
        # may claim: the primitive drops the rows of an empty album and of a
        # ghost whose folder is already gone without relocating a byte.
        mutated = 0
        moved = 0
        # Every folder the fan-out would relocate WHOLE is asked before the first
        # one moves, so the guard's own "Nothing was moved." is a property of the
        # operation. Asked per album inside the loop it was not: an album in
        # position two refused as the partial 500, in a sentence that says one
        # album HAD been moved. Only the whole-folder arm is asked, because the
        # per-item fallback never reaches that guard and a flat library, where
        # every album root is the music dir, would otherwise refuse every album.
        for album_id in album_ids:
            album = lib.get_album(album_id)
            root = None if album is None else whole_folder_root(lib, album)
            if root is not None:
                refuse_protected_tree(root, protected, action="moved")
        with lib.transaction():
            for album_id in album_ids:
                album = lib.get_album(album_id)
                if album is None:
                    continue
                if dropped_item_ids is not None:
                    dropped_item_ids.update(_require_id(i.id) for i in album.items())
                try:
                    dest = trash_album_folder(
                        lib,
                        album,
                        trash_dir=trash_dir,
                        origins_dir=origins_dir,
                        protected=protected,
                    )
                except Exception as exc:
                    # ONE arm, for every cause. It used to be two, and the
                    # LibraryRootUnavailableError half reported "the music share
                    # became unavailable" as fact — a diagnosis the error it
                    # caught had not made (``require_library_present`` refuses
                    # for a dropped share OR for music files removed outside
                    # MusicDrop, and its message says both). The remaining arm
                    # relays the cause instead of naming it, which is true for
                    # both, and a permission error, a full disk or a DB fault
                    # gets the same two tiers rather than a bare message with no
                    # idea how far the delete had got.
                    #
                    # The message says "the albums it never reached are
                    # untouched", and it says that rather than "the rest"
                    # because the album it stopped ON can be touched. The
                    # ``album.remove`` window used to be how: beets deletes the
                    # album row and THEN sends ``album_removed`` to plugins with
                    # no try/except around the handlers, so a listener that
                    # raised left that album's folder in Trash with its row
                    # gone. The primitive now moves that folder BACK
                    # (``decisions.md`` 28 item 4), which does not make the
                    # album untouched — its rows can be half-removed and beets
                    # commits that on the way out — it only means the files are
                    # no longer somewhere the message never mentions. Neither
                    # counter below has counted that album either way: both
                    # count returns from the primitive, so the fan-out cannot
                    # name it, which is why the message speaks only of the ones
                    # it never got to.
                    #
                    # A move that fails PART-WAY sits outside that window in the
                    # other direction — the rows are KEPT, which is the safe
                    # side. Two of the ways in: ``trash.py``'s post-condition
                    # re-checks the root BEFORE it checks whether anything
                    # landed, so a share dropping mid-move raises with some items
                    # already under the Trash container, and a cross-filesystem
                    # ``shutil.move`` is copy-then-delete, so a failure between
                    # the two leaves the folder at both ends. Both are relayed
                    # in the cause's own words, which is all this end can offer:
                    # the second names the paths it was working on, the first
                    # names the mount rather than the half-moved album.
                    if mutated == 0:
                        # Nothing mutated yet, so the caller's own error still
                        # holds and the 503 tier stays open. It does NOT follow
                        # that nothing MOVED: the window above reaches here as
                        # well, since ``mutated`` counts returns and not rows.
                        raise
                    raise _partial(exc, moved=moved, mutated=mutated, total=len(album_ids)) from exc
                mutated += 1
                if _reached_trash(dest, trash_dir):
                    moved += 1
    return DeleteResult(trashed_albums=len(album_ids), trash_path=str(trash_dir))


def _reached_trash(dest: str, trash_dir: Path) -> bool:
    """Whether :func:`~app.beets.trash.trash_album_folder`'s answer names a real
    Trash ENTRY — i.e. whether that album's files actually moved.

    The primitive returns ``str(trash_dir)`` itself from both branches that drop
    an album's rows having relocated nothing (no item rows at all; a ghost whose
    folder is already gone), and a path strictly INSIDE ``trash_dir`` whenever
    something really moved. The shared-folder fallback's own ghost arm returns
    the album's music-dir folder, which is outside Trash and so reads as "not
    moved" too — the check is "is it in Trash", not "is it different".

    That return value is the only signal the caller has, and the fan-out's
    partial-failure message may only claim what it can show.
    """
    entry = Path(os.path.normpath(dest))
    root = Path(os.path.normpath(trash_dir))
    return entry != root and entry.is_relative_to(root)


def _partial(exc: Exception, *, moved: int, mutated: int, total: int) -> ArtistDeletePartialError:
    """The fan-out's partial-progress message, in the two shapes it can be true in.

    "N of M albums had been moved to Trash" is simply false for a run that only
    dropped ghost rows, and it is the sentence a user reads before going to look
    for their files — so a run with nothing in Trash says what it did instead,
    and the 500 it becomes drops the recovery line that would send them there.

    Both shapes speak only of the albums this fan-out finished with: the counts
    are of returns from the primitive, so neither says anything about the album
    it stopped ON. That silence is deliberate — the album it stopped on is the
    one case nothing here can observe (see :func:`_recovery`), and the second
    shape used to fill it in with "nothing reached the Trash folder", which is
    the album.remove window's exact opposite.

    The closing clause is qualified for the same reason. It read "the rest are
    untouched", and "the rest" takes in the album this stopped on, which can be
    the most touched of all. The clause is STILL qualified now that the
    whole-folder path undoes its own ``album.remove`` window
    (:class:`~app.beets.trash.TrashRowsNotRemovedError`), because that undo
    narrows the set rather than emptying it: the album can be half-moved with
    its rows kept, it can have come back from Trash with the library's memory of
    it already gone, and on the per-item path (a shared folder) it can be listed
    with its files inside the Trash container. "Untouched" is false in all
    three, and none of them is a state either counter can see.
    """
    if moved:
        return ArtistDeletePartialError(
            f"the delete stopped after {moved} of {total} albums had been moved to Trash;"
            f" the albums it never reached are untouched ({exc})",
            moved=moved,
            cause=exc,
        )
    return ArtistDeletePartialError(
        f"the delete stopped after dropping {mutated} of {total} albums that had no files"
        f" left to move; the albums it never reached are untouched ({exc})",
        moved=0,
        cause=exc,
    )


def _gate() -> None:
    """Refuse (409) while any library job that mutates the library is running.

    Same set as config Apply / duplicates resolve — a delete tears at the same
    files + DB those jobs touch, so it must not overlap them.
    """
    if library_job_active():
        raise HTTPException(
            status_code=409,
            detail="A library operation is in progress; delete available when it finishes",
        )


def _recovery(exc: Exception) -> str:
    """The 500's recovery hint. It may PROMISE Trash only if something is IN Trash.

    Asked the other way round from how it started, because listing the failures
    that moved nothing kept missing one. Exactly ONE failure here is KNOWN to
    have a Trash entry — a fan-out that got past its first album and really
    relocated files (``ArtistDeletePartialError`` with ``moved``) — so that is
    the arm that states Trash as a fact, and everything else falls to a hint that
    does not. Enumerating the other direction meant a new "moved nothing" path
    was silently welcomed into the promise: a fan-out that fails on its FIRST
    album re-raises the cause bare (nothing mutated, so the caller's own error
    still holds), which is neither of the two cases the old list named, and the
    user of a delete that touched nothing was sent to look in a Trash folder that
    had never been created.

    The five states, and the sentence each gets:

    * a partial fan-out with files in Trash — the only Trash promise, and it
      yields to the fourth bullet when the album the fan-out stopped ON is in
      that state: the promise about the albums before it is true, but it is not
      the sentence that reader needs first;
    * ``TrashMoveIncompleteError``, raised precisely BECAUSE the files did not
      move; its own message already says the library rows were kept, so the hint
      says where the album still is;
    * ``TrashRowsNotRemovedError`` — ``album.remove`` raised after the whole
      folder had moved, and the folder was moved BACK. This one used to be the
      worst inhabitant of the fallback below: the folder sat in Trash with its
      origin record written and the album row already gone (beets deletes it and
      THEN sends ``album_removed`` to plugins, ``beets/library/models.py:391-394``,
      wrapping no handler in try/except, ``beets/plugins.py:614-627``), so the
      Trash page offered an exact move-back on an entry whose owner this line
      was telling there was nothing to look for — with Empty one click away.
      Owner ruling ``decisions.md`` 28 item 4 closed it at the source, and the
      sentence now says where the files really are: back in the music folder;
    * ``TrashDeleteIncompleteError`` — that undo failed too. Its own message is
      composed from the disk and names both paths, so this line's whole job is
      to stop the reader emptying Trash before they have read it. Reached
      through ``ArtistDeletePartialError.cause`` as well as bare: wrapped, it
      used to be answered with the Trash promise above, which is the one
      instruction this state must not give;
    * everything else — the arm that cannot know, so it ASKS rather than tells.
      Most of what lands here moved nothing: a fan-out stopped before its first
      album, one whose albums were all ghosts or empty rows, most faults inside a
      single-album delete. What is left in it that DID leave bytes under Trash is
      now one case rather than two — a move that stops PART-WAY, which keeps the
      rows: a cross-filesystem ``shutil.move`` is copy-then-delete and a failure
      between the two leaves the bytes at both ends, and the per-item fallback
      moves item by item, so a fault mid-loop (or a share dropping there — see
      :func:`~app.beets.trash._require_move_happened`) leaves some of them under
      the Trash container. That fallback is also the one the per-item mover's own
      ``album.remove`` window lands in: ``trash_album`` commits each item's path
      INTO the Trash container before it removes the rows, and it gets no undo
      (see :func:`~app.beets.trash.trash_album`), so this sentence still has to
      ask rather than tell.

    So the fallback names Trash as a place to CHECK, and stops there. It used to
    read the answer out for the user as well — "if the album's folder is there it
    can be restored from there; if it is not, nothing moved and there is nothing
    to restore" — and the second bullet's state falsifies both halves at once.
    Measured on 2026-09-02, with ``Item.move`` raising on the second item of an
    album in a shared folder (``tests/test_delete.py``'s
    ``_shared_folder_two_track_library``): what sits in Trash is the CONTAINER,
    holding the one item that made it, not the album's folder; no origin record
    was written, because ``trash_album`` writes
    one only after the move; and the album is still in the library with that
    item's row pointing inside Trash. Telling that user their files can be
    restored from Trash is as wrong as telling the previous one there is nothing
    there. Naming Trash as a place to look is one look for the user who moved
    nothing, against a lost album for the user who did.
    """
    if isinstance(exc, ArtistDeletePartialError):
        # Read THROUGH the wrapper first: the album this stopped on can be in a
        # state whose own line is the one that matters more than the promise
        # about the albums before it. Only the double failure qualifies — the
        # others below either kept their rows (``TrashMoveIncompleteError``) or
        # left nothing in Trash (``TrashRowsNotRemovedError``), so the fan-out's
        # own promise is still the more useful sentence for those.
        if isinstance(exc.cause, TrashDeleteIncompleteError):
            return _DO_NOT_EMPTY_TRASH
        if exc.moved:
            return "Files are recoverable in the Trash folder. Retry."
    if isinstance(exc, TrashMoveIncompleteError):
        return "The files were not moved and the library still has the album. Retry."
    if isinstance(exc, TrashRowsNotRemovedError):
        return (
            "The files were moved back, so there is nothing in Trash for this album."
            " Check whether the album is still listed before retrying."
        )
    if isinstance(exc, TrashDeleteIncompleteError):
        return _DO_NOT_EMPTY_TRASH
    return (
        "Check the Trash folder before retrying: a delete that stops part-way can"
        " leave some or all of the files there. Retry."
    )


def _checked_store(app: FastAPI) -> tuple[LibraryHandle, Path, Path, ProtectedTrees]:
    """The handle and the CHECKED Trash / origin-store pair, or a 503.

    ``resolve_trash_dir`` follows whatever the configured path points at NOW, so
    the boot-time containment check says nothing about this request: a symlink
    dropped at the Trash path after startup was measured to redirect a whole
    delete into the music library. The pair is taken here, once, and handed to
    the mover.

    503 and not 500: the same tier — and the same "nothing was moved" promise —
    that the unusable-store and unmounted-share guards above use, because this
    one also fires before anything moves or is dropped. Raised inline so the
    status stays a literal tests/test_route_status_declarations.py can see.
    """
    handle: LibraryHandle = app.state.beets_library
    settings = _settings(app)
    try:
        trash_dir, origins_dir = checked_store_dirs(settings, handle)
    except StoreLayoutError as exc:
        raise HTTPException(status_code=503, detail=_NOTHING_DELETED_LAYOUT.format(exc)) from exc
    protected = checked_protected_trees(
        settings, handle, trash_dir=trash_dir, origins_dir=origins_dir
    )
    return handle, trash_dir, origins_dir, protected


def _failed(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"message": f"Delete failed: {exc}", "recovery": _recovery(exc)},
    )


async def delete_album_op(
    request: Request, album_id: int, dropped_item_ids: set[int] | None = None
) -> DeleteResult:
    """Async wrapper for :func:`delete_album`: gate + swap-lock + threadpool.

    ``dropped_item_ids`` is passed straight through so the endpoint can re-export
    the `.m3u8` of every playlist holding a track this delete removed. It is only
    ever read on the success path: every failure exit here raises, so a 503/500
    caller never re-exports off a delete that did not (fully) happen.
    """
    app = request.app
    _gate()
    async with _swap_lock(app):
        handle, trash_dir, origins_dir, protected = _checked_store(app)
        try:
            return await run_in_threadpool(
                delete_album,
                handle.lib,
                album_id,
                trash_dir=trash_dir,
                origins_dir=origins_dir,
                protected=protected,
                dropped_item_ids=dropped_item_ids,
            )
        except AlbumNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Ahead of the blanket except on purpose: this cause has one thing that
        # is known on every path here — the rows are still in the library — and
        # ``_failed``'s structured body is built to carry a per-failure recovery
        # line it does not need. The ordinary shape is a share that was already
        # gone, where the guard fires before anything moves or is dropped; it is
        # not the only shape, since the same error comes from ``trash.py``'s
        # post-condition with part of an album already under the Trash container
        # and its rows kept, and there the error's own words name the mount
        # rather than that album (see :func:`delete_artist`). What the two share
        # is the library, which is what the route's 503 description states and
        # all it states. 503 with the guard's own flat sentence, matching what
        # the disk-sync preview already answers for this same cause
        # (app/api/disk_sync.py). Raised inline rather than through a helper like
        # ``_failed`` so the status stays a literal that
        # tests/test_route_status_declarations.py can see.
        except LibraryRootUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # The second setup fault, and the same tier for the same reason: the
        # store is checked before anything moves or is dropped, so the one thing
        # known on every path here is that the album is still in the library —
        # which is what the route's 503 description states. Falling through to
        # the 500 would attach ``_recovery``'s "check the Trash folder" to a
        # refusal that created no Trash folder. Raised inline, not through
        # ``_failed``, so the status stays a literal
        # tests/test_route_status_declarations.py can see. The promise is
        # appended HERE and not carried by the store's own sentence, because
        # every mover relays that sentence — including the ones that reach it
        # having already moved albums (see ``_NOTHING_DELETED``).
        except TrashOriginsStoreUnusableError as exc:
            raise HTTPException(status_code=503, detail=exc.worded_with(_NOTHING_DELETED)) from exc
        # The identity guard, same tier and same promise: it runs before the
        # folder moves, so nothing has been dropped. A fan-out past its first
        # album re-raises as ArtistDeletePartialError and takes the 500 below.
        except ProtectedTreeError as exc:
            raise HTTPException(status_code=503, detail=f"{exc} {_NOTHING_DELETED}") from exc
        except Exception as exc:
            raise _failed(exc) from exc


async def delete_artist_op(
    request: Request, artist_name: str, dropped_item_ids: set[int] | None = None
) -> DeleteResult:
    """Async wrapper for :func:`delete_artist`: gate + swap-lock + threadpool.

    ``dropped_item_ids`` behaves as in :func:`delete_album_op`. Note the fan-out's
    PARTIAL failure (``ArtistDeletePartialError`` -> 500) still leaves the albums
    it already trashed out of any re-export: the endpoint never gets to run the
    collateral. Nothing here can fix that — the response is an error, so there is
    no result to carry a count on.
    """
    app = request.app
    _gate()
    async with _swap_lock(app):
        handle, trash_dir, origins_dir, protected = _checked_store(app)
        try:
            return await run_in_threadpool(
                delete_artist,
                handle.lib,
                artist_name,
                trash_dir=trash_dir,
                origins_dir=origins_dir,
                protected=protected,
                dropped_item_ids=dropped_item_ids,
            )
        # Same 503-before-the-blanket-500 ordering as delete_album_op above,
        # and it matters more here: this one fans across every album.
        except LibraryRootUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # Its own arm, not a shared helper, for the reason the pair above has
        # one each: the route-status census reads the RAISE, so a missing arm
        # here leaves a declared 503 nothing produces. It carries the same
        # appended promise, and this is the fan-out where NOT putting it in the
        # shared store sentence matters: once one album has been dropped the
        # cause is re-raised as ArtistDeletePartialError and answered by the 500
        # below, whose message names how far the fan-out got.
        except TrashOriginsStoreUnusableError as exc:
            raise HTTPException(status_code=503, detail=exc.worded_with(_NOTHING_DELETED)) from exc
        # The identity guard, same tier and same promise: it runs before the
        # folder moves, so nothing has been dropped. A fan-out past its first
        # album re-raises as ArtistDeletePartialError and takes the 500 below.
        except ProtectedTreeError as exc:
            raise HTTPException(status_code=503, detail=f"{exc} {_NOTHING_DELETED}") from exc
        except Exception as exc:
            raise _failed(exc) from exc
