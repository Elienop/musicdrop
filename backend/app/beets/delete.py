"""Delete (reversible Trash) for whole albums and whole artists.

Front-door delete: move an album's — or every album of an artist's — OWN files
to Trash and drop it from the library, built on the per-item
:func:`app.beets.trash.trash_album` (owner ruling ``decisions.md`` 58: per file,
not per folder). That is beets' own ``Album.move``, so what travels is what
beets tracks — every item from its OWN stored path, plus ``album.artpath`` — and
:func:`_trash_one` adds the lyric sidecars MusicDrop wrote beside those tracks.
Anything else in the folder is a stranger's and stays, folder included.

Moving the FOLDER is what made the released case-insensitive loss reachable: two
albums can share one real directory under two spellings, and the folder move
took both (measured, ``dangling_rows = 2``). Asking each item where it lives
answers 0.

The async ``_op`` functions mirror the duplicates-resolve op: gated behind the
SAME library-job lock + 409 (Apply / import / lyrics / artist-art / reorganize),
run the blocking move in a threadpool, and bind ``music_dir_context`` so beets'
relative item paths resolve on the worker thread (which doesn't inherit the
``beets.context`` ContextVar — same gap as resolve/Apply).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from beets.library import Library
from beets.util import prune_dirs
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
from app.beets.protected import ProtectedTrees, is_one_of_ours
from app.beets.sidecars import move_sidecars
from app.beets.store_layout import StoreLayoutError, checked_protected_trees, checked_store_dirs
from app.beets.trash import TrashMoveIncompleteError, album_folder, trash_album
from app.beets.trash_origins import TrashOriginsStoreUnusableError, require_usable_store
from app.library_busy import library_job_active
from app.models.delete import DeleteResult

#: The promise the two 503 arms below add to the origin-store refusal, and the
#: ONLY place it may be made. The store's own sentence carries no such clause
#: (``trash_origins._STORE_FIX``): every mover relays that sentence, and the
#: artist fan-out and duplicates' resolve-all reach it having already moved
#: albums into Trash and dropped their rows — measured, a 500 that named one
#: album moved to Trash and then said nothing had been deleted. Here it is a
#: fact and not a hope: both arms sit above the transaction's first mutation.
#: Two arms reach a row drop and each guards first, checked structurally by
#: tests/test_delete.py: ``trash_album``'s body opens with its two guards and
#: nothing else (``require_library_root`` then ``require_usable_store``, ahead of
#: its ``mkdir``, of the move, and of the ghost arm that drops rows having
#: relocated nothing), and :func:`_trash_one`'s no-item-rows arm calls
#: ``require_usable_store`` before its own ``album.remove``. So the refusal
#: reaches the first album and no further.
_NOTHING_DELETED = "Nothing has been deleted."

#: The layout refusal's own wording, and it can make the same promise for the
#: same reason: ``_checked_store`` runs before the first mutation of either op,
#: so the delete stops at the check with nothing moved and no row dropped.
_NOTHING_DELETED_LAYOUT = "{} " + _NOTHING_DELETED


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
    nothing at all (an album with no item rows, and a ghost whose files are all
    gone). Counted on the primitive's RETURN, so it is a floor and not a census:
    the album this stopped on is never in it, and it can have files in Trash all
    the same. :func:`_recovery` names the ways this file can point at, not the
    whole list; nothing here distinguishes them from a failure that moved
    nothing, which is why that hint asks rather than tells. Two ways in: a move
    that stops part-way, and the row-drop window — ``trash_album`` commits each
    item's path INTO the Trash container before it removes the rows, and it gets
    no undo (owner ruling ``decisions.md`` 58 replaces 28 item 4's move-back
    with one primitive that never had one).
    """

    def __init__(self, message: str, *, moved: int) -> None:
        super().__init__(message)
        self.moved = moved


def delete_album(
    lib: Library,
    album_id: int,
    *,
    trash_dir: Path,
    origins_dir: Path,
    protected: ProtectedTrees,
    dropped_item_ids: set[int] | None = None,
) -> DeleteResult:
    """Move one album's own files to Trash and drop it. 404 on unknown id.

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
            trash_path = _trash_one(
                lib, album, trash_dir=trash_dir, origins_dir=origins_dir, protected=protected
            )
    return DeleteResult(trashed_albums=1, trash_path=trash_path)


def _trash_one(
    lib: Library, album: Any, *, trash_dir: Path, origins_dir: Path, protected: ProtectedTrees
) -> str:
    """:func:`~app.beets.trash.trash_album`, plus what the front door owes.

    Three additions, all around the primitive rather than inside it, because the
    primitive's other callers (duplicates resolve, import Replace) want none of
    them:

    * **the lyric sidecars go with their tracks.** ``Album.move`` carries what
      beets tracks, which is the items and ``album.artpath``; the ``.lrc``/
      ``.txt`` files are MusicDrop's own, written beside the audio by
      :mod:`app.beets.lyrics`. Left behind they sit in a now audio-empty folder
      the reorganize orphan sweep trashes separately, which is how the deleted
      album's lyrics end up in two Trash entries. Replace is the caller that
      must NOT do this: the new album lands on the same file names and inherits
      the sidecars, so carrying them would lose lyrics the user still has.
    * **the folder the carry emptied is pruned.** beets prunes after its own
      moves, and with a sidecar still in the folder it correctly decides to keep
      it — then the carry takes that sidecar and beets is not going to be asked
      again. Measured: deleting an album with one ``.lrc`` left an empty
      ``$albumartist/$album`` behind, which every album with lyrics would.
      ``prune_dirs`` is beets' own, called with beets' own root, so this is the
      step beets would have taken had it known about the file: it breaks at the
      first ancestor that is not empty (so a stranger's booklet keeps the
      folder), it never removes ``root`` itself (``ancestry`` excludes the path,
      so a FLAT library is a no-op), and it swallows its own ``OSError``.
    * **a folder that IS one of ours is put back, and never pruned.** beets'
      prune already rmtree'd every ancestor its move emptied, up to
      ``lib.directory`` — measured on an album imported in place into the inbox,
      that removed the inbox AND the folder above it. Asked BEFORE the move,
      since afterwards there is nothing left to stat, and only about the album's
      own folder: with the files moved item by item, a folder that merely HOLDS a
      store is never touched (so nothing here refuses, where the whole-folder
      mover had to).

    All three are best-effort in the sense that matters: they run after the
    primitive returned, and ``move_sidecars`` never raises, so none can fail or
    undo a delete that has already happened.

    An album with NO item rows is answered here instead of by the primitive,
    which would ``mkdir`` a container for it and return that path: an empty Trash
    entry the page lists as a 0-track row, and one :func:`_reached_trash` reads
    as "moved" (measured). Near-unreachable — beets prunes an album when its last
    item goes — but not free: inside the fan-out such a row can be dropped and a
    later album abort the run, which is part of why that caller reports partial
    progress rather than "nothing moved". The store guard runs ahead of it
    because this arm DROPS A ROW (see ``_NOTHING_DELETED``); the root guard does
    not, deliberately, since no item rows means no files and no folder, so there
    is no on-disk state a mount could protect.
    """
    items = list(album.items())
    if not items:
        require_usable_store(origins_dir)
        album.remove(delete=False)
        return str(trash_dir)
    root = album_folder(lib, items)
    ours = bool(root) and is_one_of_ours(root, protected)
    moved_audio: list[tuple[str, str]] = []
    trash_path = trash_album(
        lib, album, trash_dir=trash_dir, origins_dir=origins_dir, moved_audio=moved_audio
    )
    for old_audio, new_audio in moved_audio:
        move_sidecars(old_audio, new_audio)
    if ours:
        Path(root).mkdir(parents=True, exist_ok=True)
    else:
        prune_dirs(os.fsencode(root), lib.directory)
    return trash_path


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
      raises ``TrashOriginsStoreUnusableError`` ahead of every branch that drops
      a row (see ``_NOTHING_DELETED``), so it reaches the first album and no
      further. The op answers either with a 503. Nothing
      has been DROPPED whenever that fires; nothing has moved either, unless the
      share went during one album's own move, which the primitive catches with
      that album's rows kept and some of its files under the Trash container;
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
        # first. Cheap (one isdir + one scandir entry) against N album moves.
        require_library_root(lib)
        # NO second pre-check for the origin store here, deliberately, and the
        # asymmetry with the line above is the point. ``require_library_root``
        # earns its place because ``trash_album`` only re-checks the ROOT inside
        # one branch, so without it a fan-out could reach its second album
        # before anything refused. ``require_usable_store`` needs no such help:
        # every branch that drops a row asks it first (``_NOTHING_DELETED``), so
        # the first album already refuses with nothing dropped. A copy here
        # changes no outcome any test can see: adding it back left the whole
        # suite green (measured). That is not an argument that the
        # delete tests would have caught one if it did, and the difference has
        # been measured too — ``origin_recorded``'s refusing arm survives
        # tests/test_delete.py + tests/test_trash.py and is killed
        # only in tests/test_trash_origins_store.py, so this file's own pins are
        # not where every delete-path guard lives. It would also refuse a
        # fan-out over an artist with NO albums, which mutates nothing at all.
        #
        # NO identity pre-check either, and that one is a removal: the guard the
        # whole-folder mover needed ("this folder HOLDS one of our stores, so it
        # has no safe relocation") has no question to answer once each item moves
        # from its own path — measured, a store held inside an album folder
        # survives the per-item delete untouched. What is left of the identity
        # question is :func:`_trash_one`'s, asked per album and answered by
        # re-creating the directory rather than by refusing.
        #
        # Two counters, because they answer different questions and a run can
        # have one without the other. ``mutated`` is albums whose ROWS are gone,
        # which is what makes the 503's "nothing was dropped" false and so
        # decides which tier a fault gets. ``moved`` is albums whose FILES are
        # in Trash, which is the only thing the message and the recovery hint
        # may claim: an album with no item rows and a ghost whose files are all
        # gone both have their rows dropped without a byte relocating.
        mutated = 0
        moved = 0
        with lib.transaction():
            for album_id in album_ids:
                album = lib.get_album(album_id)
                if album is None:
                    continue
                if dropped_item_ids is not None:
                    dropped_item_ids.update(_require_id(i.id) for i in album.items())
                try:
                    dest = _trash_one(
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
                    # ``album.remove`` window is how: beets deletes the album row
                    # and THEN sends ``album_removed`` to plugins with no
                    # try/except around the handlers, so a listener that raises
                    # leaves that album's files in the Trash container with its
                    # row gone. There is no undo — ``trash_album`` rewrote every
                    # stored path by TEMPLATE on the way in, so putting it back
                    # is not one move (see that function). Neither counter below
                    # has counted that album either way: both count returns from
                    # the primitive, so the fan-out cannot name it, which is why
                    # the message speaks only of the ones it never got to.
                    #
                    # A move that fails PART-WAY sits outside that window in the
                    # other direction — the rows are KEPT, which is the safe
                    # side. Two of the ways in: ``trash.py``'s post-condition
                    # re-checks the root BEFORE it checks whether anything
                    # landed, so a share dropping mid-move raises with some items
                    # already under the Trash container, and the move runs item
                    # by item, so a fault mid-loop leaves the album's files at
                    # both ends. Both are relayed in the cause's own words, which
                    # is all this end can offer: the first names the mount rather
                    # than the half-moved album.
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
    """Whether :func:`_trash_one`'s answer names a real Trash ENTRY — i.e.
    whether that album's files actually moved.

    Both arms that drop an album's rows having relocated nothing answer outside
    ``trash_dir``: the no-item-rows arm returns ``str(trash_dir)`` itself, and
    ``trash_album``'s ghost arm returns the album's own music-dir folder. So the
    check is "is it strictly IN Trash", not "is it different" — an entry equal to
    the root is not one, and a path outside it is not one either.

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
    the most touched of all: it can be half-moved with its rows kept, and it can
    be listed with every one of its files inside the Trash container (the
    ``album.remove`` window, which has no undo). "Untouched" is false in both,
    and neither is a state either counter can see.
    """
    if moved:
        return ArtistDeletePartialError(
            f"the delete stopped after {moved} of {total} albums had been moved to Trash;"
            f" the albums it never reached are untouched ({exc})",
            moved=moved,
        )
    return ArtistDeletePartialError(
        f"the delete stopped after dropping {mutated} of {total} albums that had no files"
        f" left to move; the albums it never reached are untouched ({exc})",
        moved=0,
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

    The three states, and the sentence each gets:

    * a partial fan-out with files in Trash — the only Trash promise;
    * ``TrashMoveIncompleteError``, raised precisely BECAUSE the files did not
      move; its own message already says the library rows were kept, so the hint
      says where the album still is;
    * everything else — the arm that cannot know, so it ASKS rather than tells.
      Most of what lands here moved nothing: a fan-out stopped before its first
      album, one whose albums were all ghosts or empty rows, most faults inside a
      single-album delete. Two things in it DID leave bytes under Trash. A move
      that stops PART-WAY keeps the rows, so some of the album's files are under
      the container and the rest are in the library (a share dropping mid-move —
      see :func:`~app.beets.trash._require_move_happened`). And the
      ``album.remove`` window: ``trash_album`` commits every item's path INTO the
      container before it removes the rows, and it gets no undo, so a raise there
      leaves the album listed with all of its files in Trash. Owner ruling
      ``decisions.md`` 58 replaces 28 item 4's whole-folder move-back with this
      one primitive, so this sentence is now what the row-removal failure gets —
      and it has to ask rather than tell, because none of these is a state this
      end can observe.

    So the fallback names Trash as a place to CHECK, and stops there. It used to
    read the answer out for the user as well — "if the album's folder is there it
    can be restored from there; if it is not, nothing moved and there is nothing
    to restore" — and a half-moved album falsifies both halves at once. Measured
    on 2026-09-02, with ``Item.move`` raising on the second item of a two-track
    album (``tests/test_delete.py``'s ``_shared_folder_two_track_library``): what
    sits in Trash is the CONTAINER, holding the one item that made it, not the
    album's folder; no origin record was written, because ``trash_album`` writes
    one only after the move; and the album is still in the library with that
    item's row pointing inside Trash. Telling that user their files can be
    restored from Trash is as wrong as telling the previous one there is nothing
    there. Naming Trash as a place to look is one look for the user who moved
    nothing, against a lost album for the user who did.
    """
    if isinstance(exc, ArtistDeletePartialError) and exc.moved:
        return "Files are recoverable in the Trash folder. Retry."
    if isinstance(exc, TrashMoveIncompleteError):
        return "The files were not moved and the library still has the album. Retry."
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
    # One arm for both: ``checked_protected_trees`` CREATES the Trash when it is
    # absent, and refuses the same way when it cannot.
    try:
        trash_dir, origins_dir = checked_store_dirs(settings, handle)
        protected = checked_protected_trees(
            settings, handle, trash_dir=trash_dir, origins_dir=origins_dir
        )
    except StoreLayoutError as exc:
        raise HTTPException(status_code=503, detail=_NOTHING_DELETED_LAYOUT.format(exc)) from exc
    # The guard the ops below reach anyway, now reachable HERE too: creating a
    # Trash inside a library whose music is not there leaves a directory on a
    # bare mountpoint that defeats the cheap mounted-check for every later
    # caller (security seat H-1). Same tier and the same promise — it fires
    # before anything moves.
    except LibraryRootUnavailableError as exc:
        raise HTTPException(status_code=503, detail=_NOTHING_DELETED_LAYOUT.format(exc)) from exc
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
        except Exception as exc:
            raise _failed(exc) from exc
