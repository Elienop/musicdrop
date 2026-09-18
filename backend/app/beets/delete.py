"""Delete (reversible Trash) for whole albums and whole artists.

Front-door delete: move an album's — or every album of an artist's — OWN files
to Trash and drop it from the library, built on the per-item
:func:`app.beets.trash.trash_album` (owner ruling ``decisions.md`` 58: per file,
not per folder). That is beets' own ``Album.move``, so what travels is what
beets tracks — every item from its OWN stored path, plus ``album.artpath`` — and
:func:`_trash_one` adds the lyric sidecars MusicDrop wrote beside those tracks.
Anything else in the folder is a stranger's and stays. The FOLDER stays only
while something is left in it: beets prunes a directory its own move emptied,
climbing to ``lib.directory`` — so an album alone in its folder takes the folder
with it, and one of the app's own directories in that chain is put back
(:func:`_restore_our_dirs`).

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

import logging
import os
from pathlib import Path
from typing import Any

from beets.library import Library
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.beets.config_editor import _settings, _swap_lock
from app.beets.library import (
    LibraryHandle,
    LibraryRootUnavailableError,
    _abs_path,
    _coerce_str,
    _require_id,
    require_library_root,
)
from app.beets.protected import ProtectedTrees, is_one_of_ours
from app.beets.sidecars import carry_sidecars, sidecar_base
from app.beets.store_layout import StoreLayoutError, checked_protected_trees, checked_store_dirs
from app.beets.trash import TrashMoveIncompleteError, trash_album
from app.beets.trash_origins import TrashOriginsStoreUnusableError, require_usable_store
from app.library_busy import library_job_active
from app.models.delete import DeleteResult

_log = logging.getLogger(__name__)

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
#: its ``mkdir``, of the move, of the no-item-rows arm and of the ghost arm that
#: drops rows having relocated nothing), and :func:`_trash_one`'s retry arm calls
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

    Two additions around the primitive rather than inside it, because one of its
    other callers was measured to want neither and the other was not measured at
    all (see the first bullet):

    * **the lyric sidecars go with their tracks, and the folder they leave is
      re-pruned.** ``Album.move`` carries what beets tracks — the items and
      ``album.artpath``; the ``.lrc``/``.txt`` files are MusicDrop's own, written
      beside the audio by :mod:`app.beets.lyrics`. Left behind they sit in a now
      audio-empty folder the reorganize orphan sweep trashes separately, which
      splits the deleted album's lyrics across two Trash entries. This is
      :func:`~app.beets.sidecars.carry_sidecars`, per moved item, the same call
      reorganize and tag edit make — it carries, re-prunes THAT item's directory
      with the user's ``clutter:`` list, and swallows the prune's ``OSError``.
      Import Replace is the caller that must NOT do this: MEASURED, the new album
      lands on the same stem the surviving sidecar has (``Art/Alb/01 T1``), so it
      inherits the lyrics and carrying them would lose lyrics the user still has.
      Duplicates resolve was NOT measured; it is simply unchanged.
    * **every app-owned directory at or above the album root is put back.** beets
      prunes inside ``Album.move``, climbing to ``lib.directory`` and rmtree-ing
      whatever it empties, so a store the album sat in or under disappears —
      measured three ways against the whole-folder mover it replaced: the album
      root IS the inbox (multi-disc, where the root is the COMMONPATH and not the
      first item's folder), the album in a CHILD of the inbox, and a Trash inside
      the library reached through the ghost arm. Deleting our own prune does not
      fix any of them, because beets' prune is the one that climbs. So the chain
      is collected BEFORE the move (afterwards there is nothing left to stat) and
      re-created in a ``finally`` (a raise from the primitive must not leave a
      store gone). Only ever EMPTY directories are re-created: ``prune_dirs``
      breaks at the first ancestor holding anything, so a store with contents is
      never removed to begin with.

    Nothing here refuses. A folder that merely HOLDS a store was refused by the
    whole-folder mover because it relocated a TREE; moving the items one by one
    never takes the store, so a refusal would only deny the operator a delete.

    Both steps run after the primitive returned, so neither can undo a delete
    that has already happened — and neither can FAIL one either: the carry
    swallows its own errors and :func:`_restore_our_dirs` swallows the ``mkdir``'s
    (``test_a_prune_that_raises_does_not_fail_the_delete``,
    ``test_a_store_that_cannot_be_recreated_does_not_fail_the_delete``).

    The arm above them is the RETRY of a delete whose row drop raised — the one
    failure the recovery line tells the user to retry. The files are already in
    the container the first attempt filled, so a second move would allocate a
    second container, write an origin naming a path inside Trash, and leave the
    record that names the real folder on an empty husk (measured, both Trash
    layouts). What it does NOT put right is the part-way state's sidecars: the
    first attempt raised before the carry, so the ``.lrc`` files are still beside
    where the audio was, under names this end can no longer derive (the move
    re-filed every track by template). They are left for the reorganize orphan
    sweep, which takes an audio-free folder to Trash of its own accord.
    """
    items = list(album.items())
    if _all_files_already_in_trash(lib, items, trash_dir):
        # A RETRY after ``album.remove`` raised: the files are already in the
        # container a previous attempt filled, and its origin record already
        # names the album's real folder. Moving again would allocate a second
        # container, record an origin pointing INSIDE Trash, and leave the true
        # record on an empty husk (measured). Only the row drop is left.
        require_usable_store(origins_dir)
        album.remove(delete=False)
        return os.path.dirname(_abs_path(lib, items[0].path))
    ours = _our_dirs_at_or_above(lib, items, protected)
    moved_audio: list[tuple[str, str]] = []
    try:
        trash_path = trash_album(
            lib, album, trash_dir=trash_dir, origins_dir=origins_dir, moved_audio=moved_audio
        )
        for old_audio, new_audio in moved_audio:
            if _stem_is_claimed(lib, old_audio):
                # Another album's track shares this directory and stem, so the
                # sidecar is its lyrics too — measured on `01 T1.flac` and
                # `01 T1.mp3` in one folder, where deleting the first took the
                # second's `.lrc`. Same argument as Replace's.
                continue
            carry_sidecars(lib, old_audio, new_audio)
    finally:
        _restore_our_dirs(ours)
    return trash_path


def _all_files_already_in_trash(lib: Library, items: list[Any], trash_dir: Path) -> bool:
    """Whether every one of this album's item rows names a file that IS in Trash.

    The state a previous attempt leaves when ``Album.move`` finished and
    ``Album.remove`` then raised: the files are in a container that already has
    an origin record naming the album's real folder. Same string test
    ``trash._moved_under`` makes, plus one ``isfile`` per row.

    The stat is what keeps a GHOST out — rows naming files in Trash that are not
    there. A retry may skip ``require_library_root`` (the files are in Trash, so
    the music share says nothing about dropping those rows); a ghost may not,
    because "every file is missing" is exactly what an unmounted share looks
    like, and the guard's own message says it cannot tell the two apart. Pinned
    by ``test_rows_inside_trash_with_their_files_gone_still_meet_the_root_guard``,
    which is the only test that separates the two arms.

    Its remaining false positive is an album whose files genuinely LIVE inside
    the Trash folder, which needs Trash inside the music dir and an import from
    it. That album gets its rows dropped and its files left where they already
    were, in Trash — no origin record, so Restore offers a re-import rather than
    a move-back. Nothing is lost, which is why it is not paid for with a third
    check.
    """
    if not items:
        return False
    prefix = os.path.join(os.path.normpath(str(trash_dir)), "")
    paths = [os.path.normpath(_abs_path(lib, it.path)) for it in items]
    return all(p.startswith(prefix) and os.path.isfile(p) for p in paths)


def _our_dirs_at_or_above(lib: Library, items: list[Any], protected: ProtectedTrees) -> list[str]:
    """Every app-owned directory ``Album.move``'s prune could remove. Never raises.

    The set beets' own prune walks, asked the same way it asks: from each item's
    directory upward, stopping at ``lib.directory`` (``prune_dirs`` never removes
    its root, and removes NOTHING when the path is not under it —
    ``ancestry`` + the ``root in ancestors`` test, beets ``util/__init__.py``).
    Per ITEM rather than from the album root, because those are not the same
    chain: a multi-disc album's root is the commonpath of its ``Disc N`` folders,
    so a store could sit at either level.

    Cost is one ``lstat`` per DISTINCT directory in that chain — the second item
    of a normal album re-walks nothing.
    """
    stop = os.path.normpath(os.fsdecode(lib.directory))
    ours: list[str] = []
    seen: set[str] = set()
    for it in items:
        cur = os.path.normpath(os.path.dirname(_abs_path(lib, it.path)))
        while cur != stop and cur not in seen and Path(cur).is_relative_to(stop):
            seen.add(cur)
            if is_one_of_ours(cur, protected):
                ours.append(cur)
            cur = os.path.dirname(cur)
    return ours


def _restore_our_dirs(dirs: list[str]) -> None:
    """Re-create the app's own directories that the move's prune removed.

    Runs in a ``finally``: the prune happens inside ``Album.move``, so a raise
    AFTER it — the row-drop window, the post-condition — leaves the store gone
    just as a clean return does.

    Swallows its ``OSError`` for the reason the sidecar carry does: by the time
    this runs the files have moved, and a tidy-up may not turn a delete that
    happened into a delete that failed (pinned by
    ``test_a_store_that_cannot_be_recreated_does_not_fail_the_delete``).
    """
    for directory in dirs:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            _log.warning("could not re-create our own directory: %s", directory, exc_info=True)


#: Any item row sitting at the same stem as the file just moved — whose lyrics
#: the sidecar beside it could equally be. Both stored path forms, as
#: ``trash._folder_is_shared`` does it: beets stores paths relative to
#: ``lib.directory`` in the normal case and absolute for outside/legacy rows.
_STEM_CLAIMED_SQL = """
SELECT 1 FROM items WHERE substr(path, 1, ?) = ? OR substr(path, 1, ?) = ? LIMIT 1
"""


def _stem_is_claimed(lib: Library, old_audio: str) -> bool:
    """Whether a REMAINING library item is named ``<old_audio's stem>.<something>``.

    Two albums can hold ``01 T1.flac`` and ``01 T1.mp3`` in one folder, and the
    single ``01 T1.lrc`` beside them is both their lyrics — measured, deleting
    the first album carried the second's lyrics to Trash.

    Asked AFTER the primitive returned, which is what makes it cheap: the deleted
    album's own rows are already gone (``Album.remove`` is ``trash_album``'s last
    line), so any row still at the stem belongs to someone else and no self-match
    has to be filtered out. The normal single-album delete carrying its sidecar
    is the control that this has not silently become "always claimed"
    (``test_delete_album_carries_its_lyric_sidecars_into_trash``).

    Errs toward CARRYING: an unmatched path spelling answers False and the
    sidecar travels with its own track, which is this file's ordinary behaviour.
    """
    base = sidecar_base(old_audio)
    if base is None:
        return False
    abs_stem = os.fsencode(base) + b"."
    try:
        rel_stem = os.fsencode(os.path.relpath(base, os.fsdecode(lib.directory))) + b"."
    except ValueError:  # different drives — no relative spelling exists
        rel_stem = abs_stem
    with lib.transaction() as tx:
        rows = tx.query(_STEM_CLAIMED_SQL, (len(abs_stem), abs_stem, len(rel_stem), rel_stem))
    return bool(rows)


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

    So the fallback names Trash as a place that may hold the files, and names
    Empty as the thing not to do first. That second half is measured, not
    cautionary: after the ``album.remove`` window the album is listed AND every
    one of its files is in Trash, and one Empty click destroys the only copy
    (``empty_one``/``empty_all`` take no ``Library``, so nothing cross-checks the
    rows). Retry is what recovers it — :func:`_trash_one`'s retry arm drops the
    rows and leaves the files where they are — so the sentence says retry FIRST
    rather than warning and stopping. The wording it replaced read "Check the
    Trash folder before retrying", which sent that user to the one screen with a
    button that finishes the loss.

    It used to
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
        "A delete that stops part-way can leave some or all of the files in Trash."
        " Retry before emptying Trash: emptying now can destroy the only copy."
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
