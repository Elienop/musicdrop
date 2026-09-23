"""Reversible Trash relocation + shared library-album read helpers.

The single low-level "move an album to Trash and drop it from the library"
primitive, shared by the back-door /duplicates resolve op
(``app.beets.duplicates``) and the front-door duplicate-on-import Replace
action (``app.beets.import_session``). Reversible by design: files are
*relocated* (never deleted) and the DB rows dropped with ``delete=False`` —
exactly ``beet dup --move <trash> --remove`` for albums.

Every mover here also records where the folder came from, in the SIBLING store
``app.beets.trash_origins`` (``<origins_dir>/<entry name>.json``, never a file
inside the trashed folder), so Restore can put it back where it came from
instead of re-filing it by path template. Two different promises about that
record, and the difference is WHEN each is decided:

* **A store that cannot be used refuses the move**, before any ``mkdir``,
  allocation, relocation or row drop. Every mover here opens with
  ``trash_origins.require_usable_store``. Owner's ruling (``decisions.md`` 28):
  a delete that cannot record where a folder came from hands out a name whose
  record is still on disk and then loses the new one, so it must not run at all.
* **A record that fails to WRITE never fails the delete** — see
  ``trash_origins.write_trash_origin``. That write happens after the files have
  already moved, where failing would be strictly worse than the unrecoverable-
  but-completed delete it replaces.

Lives in its own module so both features import it without forming the
``import_session -> duplicates -> registry -> import_session`` cycle. Imports
only beets + the base adapter + settings (no registry/duplicates import).
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from beets.library import Library
from beets.util import bytestring_path

from app.beets.library import (
    LibraryHandle,
    _abs_path,
    _coerce_int,
    _coerce_optional_str,
    require_library_present,
    require_library_root,
)
from app.beets.protected import (
    ProtectedTreeError,
    ProtectedTrees,
    open_checked_dir,
    refuse_protected_tree,
)
from app.beets.trash_origins import (
    _NAME_MAX,
    MovedShape,
    origin_recorded,
    require_usable_store,
    write_trash_origin,
)
from app.config import Settings
from app.fsutil import BELOW_FLAGS, exists, fsync_dir
from app.wire import PLACEHOLDER, display_path

logger = logging.getLogger(__name__)


class TrashRowUnreadableError(Exception):
    """An item row has no usable ``path`` — NULL or empty — so beets cannot move it.

    Not a shortfall to tolerate the way a missing FILE is: beets' own mover reads
    ``item.path`` to build the destination, so a row without one raises out of
    ``Album.move`` with rows already committed under the container. Refused in
    :func:`trash_album` before the container is made, so nothing moved and
    nothing dropped. The predicate is FALSY, not ``is None``: beets' own
    ``lib.add(Item(title=...))`` writes ``b''`` rather than NULL, and both
    spellings are pinned
    (``test_an_album_with_a_null_path_row_refuses_before_anything_moves``).
    """


class TrashMoveIncompleteError(Exception):
    """``Album.move`` returned normally but relocated nothing.

    beets answers a source file it cannot find by logging and returning
    (``beets/library/models.py:1197-1211``), so a move that moved NOTHING is
    indistinguishable from a successful one at the call site. Dropping the DB
    rows on that is the data loss the post-condition in :func:`trash_album`
    exists to stop; this is what it raises when the music root is healthy and
    the files are still sitting where they were. NOT raised for a ghost album
    (files genuinely gone) or an unmounted share — those have their own answers,
    see :func:`_require_move_happened`. Rows are kept either way.
    """


def album_format_bitrate(items: list[Any]) -> tuple[str | None, int | None]:
    """Quality hint (format, kbps) from the album's representative (first) item."""
    if not items:
        return None, None
    first = items[0]
    fmt = _coerce_optional_str(getattr(first, "format", None))
    raw = _coerce_int(getattr(first, "bitrate", 0))  # beets stores bitrate in bps
    return fmt, (raw // 1000 if raw else None)


def album_folder(lib: Library, items: list[Any]) -> str:
    """The on-disk folder of an album, from its first item's path."""
    if not items:
        return ""
    return os.path.dirname(_abs_path(lib, items[0].path))


#: What a container is called when the text it is named from neutralises to
#: nothing. Any word does; this one reads in the Trash page's single column.
_UNNAMED_CONTAINER = "artist"


def _one_trash_level(text: str) -> str:
    """``text`` reduced to a single listable Trash container level; may be EMPTY.

    The one replace set both container-name builders share, so a tag cannot
    reach ``mkdir`` with something a display name is protected from.
    ``os.sep``, ``/`` and NUL go so "AC/DC" lands directly under the Trash dir
    instead of nesting and a NUL cannot reach the ``mkdir`` as a ``ValueError``
    no ``except OSError`` catches. :data:`app.wire.PLACEHOLDER` goes for a
    reason the other three do not share: a client string and an ID3 tag can
    both spell U+FFFD, which is what ``wire_safe`` puts in place of an
    UNDECODABLE byte, so such a container would display identically to a
    damaged sibling's name and ``wire._match_display_child`` answers 409 on
    both rows. Leading dots go because ``trash_manage._audio_free_entries``
    skips a dot-leading entry that ``empty_all`` still removes. Length is the
    allocator's job (``_unique_trash_dest``); the empty answer is the caller's,
    because the fallback word differs per caller.
    """
    for bad in {os.sep, "/", "\x00", PLACEHOLDER}:
        text = text.replace(bad, "_")
    return text.lstrip(".")


def _trash_container_name(album: Any) -> str:
    """A readable, filesystem-safe ``"<albumartist> - <album>"`` container name.

    Neutralised through :func:`_one_trash_level`, the same set
    :func:`safe_container_name` uses: a tag can carry a separator, a NUL or a
    U+FFFD just as a display name can. Both fields empty falls back to
    ``"album"`` (``_unique_trash_dest`` handles that too, but keep the intent
    explicit here).
    """
    artist = _coerce_optional_str(getattr(album, "albumartist", None)) or ""
    title = _coerce_optional_str(getattr(album, "album", None)) or ""
    name = f"{artist} - {title}".strip(" -") if (artist or title) else ""
    return _one_trash_level(name) or "album"


def safe_container_name(text: str, suffix: str) -> str:
    """``"<text><suffix>"``, as a single listable Trash container level.

    For the movers named from a display string rather than from a folder, where
    ``text`` is an artist NAME. Neutralising is :func:`_one_trash_level`'s job;
    nothing left falls back to a word.
    """
    return f"{_one_trash_level(text) or _UNNAMED_CONTAINER}{suffix}"


def _moved_under(lib: Library, items: list[Any], container: Path) -> list[Any]:
    """The items whose CURRENT stored path is inside ``container``.

    Pure string work over rows beets has already updated — no stat, no walk — so
    it costs nothing next to the moves it is checking.
    """
    prefix = os.path.join(os.path.normpath(str(container)), "")
    return [it for it in items if os.path.normpath(_abs_path(lib, it.path)).startswith(prefix)]


def _require_move_happened(
    lib: Library, album: Any, container: Path, *, items: list[Any], moved: list[Any]
) -> None:
    """Raise unless the shortfall in ``moved`` has an honest explanation.

    Called only when some item did not land under ``container``. Three arms,
    in the order that makes each failure name its own cause:

    * **root unavailable** — the share dropped between the pre-check and the
      moves, which is the whole reason a post-condition exists. Re-running the
      predicate raises ``LibraryRootUnavailableError``, so the caller still
      answers with the honest 503 and the rows stay.
    * **root healthy and the source files are genuinely GONE** — the ghost
      album. Moving nothing is correct here and dropping the rows is the point:
      the rows are all that is left, and reached through this path by the delete
      route, import Replace and duplicates resolve. Allowed through — but only once
      ``require_library_present`` has shown some other sampled FILE the library
      names is still on disk, because a dropped share with a stray entry on its
      mountpoint produces this exact state for every album at once.
    * **root healthy and the files are still SITTING THERE** — they did not
      move and nobody can say why: a permission fault on the container, a beets
      change, a bug here. Dropping the rows would be the silent data loss this
      whole post-condition exists to stop, so refuse.

    A partial move reaches neither of the last two arms: once ANY file has
    landed in the container the move demonstrably happened, so the shortfall is
    the pre-existing missing-track tolerance (one item whose file is gone makes
    beets skip that item alone). Refusing there would break duplicate resolve
    for every album carrying a missing track — a regression, not a fix.
    """
    with contextlib.suppress(OSError):
        container.rmdir()  # succeeds only while nothing landed in it
    require_library_root(lib)
    if moved:
        return
    present = [it for it in items if it.path and os.path.exists(_abs_path(lib, it.path))]
    if not present:
        # Ghost arm — the ONE exit that lets the caller drop rows having moved
        # nothing, so the root guard above is not enough here: it accepts a
        # dropped share whose local mountpoint still holds a stray entry
        # (``.stfolder``, ``lost+found``), and in that state EVERY album looks
        # exactly like this one. Ask for positive proof before conceding the
        # album is a ghost. Complementary to the raise below, not redundant with
        # it: that covers files still SITTING THERE, this covers files that are
        # all gone for the same reason.
        require_library_present(lib)
        return  # ghost album: nothing to move because nothing is there
    raise TrashMoveIncompleteError(
        f"'{_trash_container_name(album)}' did not move to Trash: {len(present)} of its"
        f" {len(items)} files are still in place and none were relocated. The library"
        f" rows were kept."
    )


def trash_album(
    lib: Library,
    album: Any,
    *,
    trash_dir: Path,
    origins_dir: Path,
    moved_audio: list[tuple[str, str]] | None = None,
) -> str:
    """Relocate one album's files under ``trash_dir`` and drop it from the library.

    Reversible: the album is moved into its OWN collision-free container dir
    directly under ``trash_dir`` (``container/$albumartist/$album/...``) via
    ``Album.move(basedir=container)``, then ``Album.remove(delete=False)`` drops
    the DB rows while leaving the files in Trash. The per-container basedir is
    what keeps two DIFFERENT albums by the SAME album-artist as two distinct
    top-level Trash entries — sharing ``trash_dir`` as the basedir collapsed them
    under one ``$albumartist`` folder, which ``list_trashed_albums`` keys on and
    the whole-folder DELETE then wiped wholesale. Returns the album's new Trash
    folder. Caller controls the transaction (so a batch can be atomic).

    Records the album's source folder on the container for display, marked
    ``moved="items"`` — this mover takes tracked files out of a folder that may
    hold other music, so Restore falls back to re-importing rather than offering
    a move-back that could put files back among a stranger's.

    ``moved_audio``, when given, collects ``(old absolute path, new absolute
    path)`` for every item that really landed under the container — the only
    moment that mapping exists, since ``Album.move`` rewrites each stored path
    and ``Album.remove`` then drops the row. Reported rather than acted on,
    because the callers disagree: ``app.beets.delete`` carries the lyric
    sidecars, import Replace must not — measured, the surviving ``01 T1.lrc``
    sits at exactly the stem ``item.destination()`` gives the new copy of the
    same album, which would lose the lyrics the user still has.

    Guarded on BOTH sides of the move, because neither half is enough alone.
    beets' ``Item.move`` (2.12 onward) silently skips a source file that is not there
    ("If the source file is missing, skip the move", ``log.warning`` then
    ``return`` — ``beets/library/models.py:1197-1211``), so with the share
    unmounted every move no-ops while ``Album.move`` still returns normally, and
    ``Album.remove`` would drop the rows anyway: a Trash path naming an empty
    folder and a library that has forgotten the album.

    * **Before** — :func:`~app.beets.library.require_library_root` raises
      ``LibraryRootUnavailableError`` if the music root is missing, empty or
      unreadable, and
      :func:`~app.beets.trash_origins.require_usable_store` raises
      ``TrashOriginsStoreUnusableError`` if the origin store cannot be looked up
      in or written to. Both run ahead of the ``mkdir`` as well as the moves, because a
      beets transaction COMMITS on the way out even while unwinding an exception
      (``beets/dbcore/db.py:940-957`` — no rollback branch), so aborting after a
      mutation would not undo it.
    * **After** — the rows are dropped only once the items' stored paths are
      provably under the container. A pre-check is point-in-time: the share can
      drop in the window between it and the first move, and then the check has
      passed on a library that is already gone. See :func:`_require_move_happened`
      for which shortfalls raise (``LibraryRootUnavailableError`` when the root is
      the cause, :class:`TrashMoveIncompleteError` otherwise) and which one is
      tolerated.

    **No undo of the ``album.remove`` window here; a stated residual.** A raise
    on that last line leaves an album the library still LISTS whose item rows
    point inside the Trash container — the state ``decisions.md`` 28 item 4
    closed while a whole-folder mover existed, by moving the one directory back.
    The undo is not one move here. Measured on a two-track album: ``Album.move``
    re-files each item under the container by PATH TEMPLATE (``<container>/
    $albumartist/$album/$track $title``, not a copy of the source layout), moves
    ``album.artpath`` with it, commits each new path as it goes, and prunes the
    source folder AND the artist folder above it — the music tree came back
    empty. Putting that back is: recreate two pruned directories, move N files
    from template paths to N recorded originals, move the art, rewrite N stored
    paths and ``artpath``, and remove the container — and a failure anywhere in
    THAT leaves the album's files split across ``/music`` and Trash with its rows
    pointing at both, which is worse than the single state it replaces. Nothing
    in this file's messages, or in ``delete._recovery``, claims the undo for this
    path; see ``BACKLOG.md``.
    """
    require_library_root(lib)
    require_usable_store(origins_dir)
    # BEFORE the move: ``Album.move`` rewrites every item's stored path, so this
    # is the last moment the album's own folder can be read off the rows.
    pre_move_items = list(album.items())
    if not pre_move_items:
        # No item rows means no files to relocate, so a container would be an
        # empty Trash entry with no origin record — measured, it listed as a
        # 0-track row and ``delete._reached_trash`` read its path as "moved".
        # Answering ``trash_dir`` says "nothing reached Trash". Behind both
        # guards above, because this arm DROPS A ROW.
        album.remove(delete=False)
        return str(trash_dir)
    if any(not it.path for it in pre_move_items):
        # FALSY, not ``is None``: beets' own ``lib.add(Item(title=...))`` stores
        # ``b''`` for a pathless item, which reached beets' mover and answered
        # 500 ``"[Errno 2] No such file or directory: ''"`` with a container made
        # and a temp file left (measured). Same predicate ``delete.py`` uses.
        #
        # Ahead of the ``mkdir``: every later step reads ``item.path``, so the
        # first one to meet the row would raise with a container on disk and
        # rows already rewritten.
        raise TrashRowUnreadableError("A track of this album has no file path in the library.")
    trash_dir.mkdir(parents=True, exist_ok=True)
    container = _unique_trash_dest(trash_dir, origins_dir, _trash_container_name(album))
    container.mkdir(parents=True, exist_ok=True)
    source_root = _album_root(lib, pre_move_items, not_in=trash_dir)
    # Keyed by item id, which the move does not change — the only handle that
    # survives ``Album.move`` rewriting every path, since the post-move objects
    # come from a fresh query.
    was_at = {it.id: _abs_path(lib, it.path) for it in pre_move_items}
    basedir = bytestring_path(str(container))
    album.move(basedir=basedir)  # relocate under the container + prune source dir
    items = list(album.items())
    # POST-CONDITION. ``Album.move`` cannot report a skip: beets logs a missing
    # source and returns (models.py:1197-1211), so "move returned" is not
    # "files moved". The pre-check above closes the window it can see; this
    # closes the one it cannot — the share dropping AFTER the check, and every
    # other cause of a silent skip. Rows are dropped only once the files are
    # provably somewhere else.
    moved = _moved_under(lib, items, container)
    if len(moved) != len(items):
        _require_move_happened(lib, album, container, items=items, moved=moved)
    if moved_audio is not None:
        # A caller carrying sidecars is protected by the RAISE above, not by this
        # line's position — a refusal never returns, so the pairs are never read
        # (measured: moving this above the post-condition changes no test).
        moved_audio.extend((was_at[it.id], _abs_path(lib, it.path)) for it in moved)
    # ``moved[0]``, not ``items[0]``: with a skipped first item the latter still
    # points into the music dir, so the returned "Trash folder" would name the
    # place the album was never moved from.
    if moved:
        first = moved[0]
    else:
        first = items[0] if items else None
    trash_path = (
        os.path.dirname(_abs_path(lib, first.path)) if first is not None else str(container)
    )
    # Recorded on the CONTAINER, not on ``trash_path``: the container is the
    # top-level Trash entry the listing keys on and the restore/empty endpoints
    # resolve, while ``trash_path`` is a template level deeper inside it.
    # ``moved="items"`` because this mover relocates tracked FILES out of a
    # folder that may hold other music — the origin is worth showing, a
    # move-back is not on offer. See ``trash_origins.MovedShape``.
    #
    # Gated on ``moved`` as well as ``source_root``: the ghost arm of
    # ``_require_move_happened`` ``rmdir``s the container and returns normally,
    # so an empty ``moved`` means there is no Trash entry to describe. That used
    # to be masked — a write INTO the deleted container failed harmlessly — but a
    # write on the /data side would succeed and leave an origin file whose name
    # nothing in Trash answers to.
    if source_root and moved:
        _record_origin(origins_dir, container, origin=source_root, moved="items")
    album.remove(delete=False)  # drop DB rows; files stay in Trash
    return trash_path


def _album_root(lib: Library, items: list[Any], *, not_in: Path | None = None) -> str:
    """The album's on-disk folder: the deepest common dir of its item files.

    Single item -> that file's directory; multi-disc -> the common ancestor of
    the ``Disc N`` subfolders (their parent ``$album`` folder).

    Rows under ``not_in`` are left out, and ``""`` when that is all of them: a
    part-way move leaves rows at both ends, whose commonpath is the parent of
    Trash AND music — ``/`` on the shipped layout, which says nothing about where
    the album came from
    (``test_a_retry_after_a_part_way_move_finishes_the_move``). Everything else
    counts, music folder or not — ``in_place`` and a symlinked album folder are
    both supported
    (``test_an_album_outside_the_music_folder_still_reaches_trash``).
    """
    dirs = [os.path.dirname(_abs_path(lib, it.path)) for it in items]
    if not_in is not None:
        skip = os.path.normpath(str(not_in))
        dirs = [d for d in dirs if not Path(d).is_relative_to(skip)]
    if not dirs:
        return ""
    return dirs[0] if len(dirs) == 1 else os.path.commonpath(dirs)


def _unique_trash_dest(trash_dir: Path, origins_dir: Path, name: str) -> Path:
    """A non-colliding ``trash_dir/<name>`` (append ``(n)`` if it already exists).

    A name counts as taken when EITHER namespace holds it. The origins half is
    what NARROWS the name key's hazard: an entry deleted outside MusicDrop (a
    file manager, an SMB client, ``docker volume rm``) leaves its record behind,
    and without this test the next album to earn that name would inherit a stale
    origin — which steers a ``rename()`` for the wrong folder. That is the exact
    hazard inode keys were rejected for, and it is strictly worse than losing an
    origin, so it is narrowed here rather than by a reaper on the LISTING, which
    would have to decide whether an empty Trash dir means "empty" or
    "unmounted". (``trash_manage.empty_all`` does sweep the whole store, but only
    once it has itself emptied Trash — a reading it does not have to guess.)

    Narrowed and not CLOSED: this function's reach is the names MusicDrop hands
    out, so a folder that arrives in ``trash_dir`` by another route — a hand
    copy, a restored backup, a sync client writing into the volume — asks the
    allocator nothing and can still land on a name whose record outlived its
    entry. That one is a stated residual; ``trash_origins``'s module docstring
    holds the full statement, and this docstring must not out-claim it.

    The OTHER half used to be a residual too and is now closed by a refusal
    rather than by this loop. The test below answered "free" for a recorded name
    whenever the store could not be reached (measured through this function as a
    non-root user with the origins dir at mode 0600: ``Dummy``, where a readable
    store gives ``Dummy (1)``). Every caller now asks
    :func:`~app.beets.trash_origins.require_usable_store` before it reaches
    here, and
    :func:`~app.beets.trash_origins.origin_recorded` raises rather than answering
    for the same fault class met in the window after that check — so this loop's
    exit is not what holds the invariant up, which is the whole reason the answer
    could not simply be flipped to "occupied" (every candidate would then read
    occupied and the loop would never end — measured, 111,939 candidates in one
    second).

    The cost of the test itself is a burnt name: after a manual deletion the
    record is litter, and an album that would have been ``<name>`` becomes
    ``<name> (1)``. Litter is the accepted price of taking that name out of the
    allocator's hands.

    **Every candidate is kept inside ``NAME_MAX``, and the occupancy test is the
    spelling that absorbs errno 36 rather than raising it** (only that one: see
    :mod:`app.fsutil`). ``Path.exists()`` does not absorb ENAMETOOLONG
    (``pathlib._IGNORED_ERRNOS`` is ENOENT/ENOTDIR/EBADF/ELOOP — errno 36 is not
    in it), so appending the first ``" (1)"`` — four bytes — turned any name of
    252 to 255 bytes into an ``OSError(36)`` escaping this function on the very
    first iteration. Measured: 251 passed, 252 and 255 raised, both with a real
    entry in the way and with only an orphaned RECORD in the way. The delete
    then 500s with nothing moved and every retry failing identically — and the
    hint that 500 carries is composed from the exception (``delete._recovery``),
    so whatever it says it can only tell the user to retry the thing that cannot
    work. The orphan sweep (whose ``except OSError`` is meant for one bad folder)
    skips such a folder in silence on every run. Shortening the HEAD to make room
    for the suffix is the answer rather than refusing or truncating elsewhere: a
    Trash entry's name is only a container, and what a restore reads to put the
    folder back is the origin RECORD, never the name. :func:`~app.fsutil.exists`
    then answers "free" instead of raising for the limits this constant cannot
    see — a filesystem with a smaller ``NAME_MAX`` (eCryptfs stops at 143
    bytes), or a Trash path close to ``PATH_MAX`` — leaving the failure to the
    statement that was trying to do the work.

    That is a smaller difference than it sounds, and the ``PATH_MAX`` half is
    where it was measured, because that one is reached by an ORDINARY delete
    with nothing injected: nest the Trash dir until ``<trash_dir>/<255-byte
    name>`` is longer than 4096 while every component still fits ``NAME_MAX``.
    Every ``mkdir`` then succeeds and the candidate cannot be looked up at all.
    Measured on this branch, ``trash_folder`` run twice over one such fixture
    from the same root — once with :func:`~app.fsutil.exists` and once with
    ``dest.exists()`` — the two ends are the same delete: ``OSError(36)``, the
    same ``.filename`` (the whole candidate path, in both), the same
    ``str(exc)``, a byte-identical 500 body (``delete._recovery``'s fallback
    arm either way), the husk still in ``/music`` and an empty Trash. Two
    differences showed, and neither reaches the user: which FRAME the traceback
    blames — ``trash_folder``'s ``shutil.move`` against this function's
    ``while`` — and one extra ``origin_recorded`` warning on the guarded side,
    where the short-circuit no longer fires so the second predicate meets the
    same errno. So
    ``test_a_trash_path_over_PATH_MAX_fails_at_the_MOVE_and_not_at_the_allocator``
    reads the frame and not the errno: in that fixture nothing else separates
    the two spellings, and the frame is what a traceback in the log has to
    point at.

    The smaller-``NAME_MAX`` half has no fixture — this suite mounts no
    filesystems — so it is forced from the predicate instead, in
    ``test_a_name_the_KERNEL_refuses_reads_as_free_and_not_as_a_500``, which
    pins the allocator's own boundary (it returns rather than raising) and
    nothing past it. What the move then does on a real such filesystem is not
    staged anywhere here.

    Long names are not only an accident of the source folder: ``beets.util``
    caps a path component it generates at 200 bytes by default
    (``MAX_FILENAME_LENGTH``, raisable via the ``max_filename_length`` config),
    but :func:`_trash_container_name` builds a name out of the album's own tags
    with no truncation at all, so it can hand this function one that is over the
    line before any suffix is added.
    """
    base = _fit_name(name or "album", _NAME_MAX)
    dest = trash_dir / base
    counter = 1
    while exists(dest) or origin_recorded(origins_dir, dest.name):
        # The suffix is ASCII, so its byte cost is its length. The head is
        # re-shortened from ``base`` every time and never from the previous
        # candidate, so reaching " (10)" takes its extra byte out of the head
        # instead of off the end of a name that already fit.
        suffix = f" ({counter})"
        dest = trash_dir / (_fit_name(base, _NAME_MAX - len(suffix)) + suffix)
        counter += 1
    return dest


def _fit_name(name: str, budget: int) -> str:
    """``name`` shortened from the end until it encodes to at most ``budget`` bytes.

    BYTES, because bytes are what the kernel limits: a CJK or fullwidth album
    title costs three bytes a character, so a 90-character name can be over the
    line while ``len()`` says it is nowhere near it.

    Whole CODEPOINTS, because every folder name here arrived through
    ``os.fsdecode``. Slicing the ENCODED form would cut a multi-byte character in
    half, which turns a name that DECODES into one that does not: the severed
    bytes come back as lone surrogates, so the entry renders with U+FFFD
    placeholders everywhere it is shown and joins the set of names a display
    string can no longer be mapped back to on its own — two of them displaying
    alike is ``AmbiguousDisplayName``, a 409 on that row's Restore and Empty
    (``app.wire._match_display_child``). Dropping trailing codepoints cannot do
    that, and it holds for a name that was ALREADY undecodable too — its bytes
    are carried as one lone surrogate each, which ``os.fsencode`` puts back as
    one byte each.

    The origin record's key is not the reason: it and the payload ``name`` are
    both taken from ``dest.name`` AFTER this has run, so they agree with the
    entry whatever this returns.
    """
    text = name[:budget]  # every codepoint costs >= 1 byte, so this bounds the loop
    while len(os.fsencode(text)) > budget:
        text = text[:-1]
    return text


def _record_origin(origins_dir: Path, dest: Path, *, origin: str, moved: MovedShape) -> None:
    """Record ``dest``'s origin, unless ``dest`` is a symlink. Never raises.

    A symlinked Trash entry is ordinary rather than hostile: ``_album_root`` is
    ``dirname(item.path)``, so an album whose own folder is a symlink into
    another volume is trashed AS a symlink because ``shutil.move`` preserves
    them. ``resolve_trash_child`` resolves the child and refuses anything landing
    outside Trash, so such a row can never be restored — and a record would make
    the listing offer "Exact restore" on a row whose Restore button 404s. Writing
    nothing keeps the promise honest: the row reads as an import-restore, exactly
    as it did before origins existed.

    (For the sidecar this refusal was a security guard — ``mkstemp(dir=entry)``
    followed the link straight out of Trash. On the ``/data`` side there is no
    such escape left; only the unkeepable promise.)
    """
    # ``Path.is_symlink`` here and ``os.path.islink`` in ``trash_manage``'s
    # listing, on purpose and not by drift — and the gap between them is wider
    # than the overlong name it is usually described by. ``Path.is_symlink``
    # swallows only ``pathlib._IGNORED_ERRNOS`` (ENOENT/ENOTDIR/EBADF/ELOOP) and
    # a ``ValueError``, re-raising every other ``lstat`` failure;
    # ``os.path.islink`` catches ``(OSError, ValueError, AttributeError)``
    # blanket and answers False to all of it. Measured on 3.12.13: on an
    # overlong name the first raises ``OSError(36)`` and the second answers
    # False; on a child of a directory at mode 0600, as a non-root user, the
    # first raises ``PermissionError(13)`` and the second still answers False.
    #
    # This site takes the raising one because ``dest`` is a path the kernel
    # accepted a statement or two ago — ``shutil.move`` in the two folder
    # movers, ``mkdir`` in the per-item one — so either fault would have failed
    # THAT step first, and reaching this line with one means the name or the
    # permissions changed inside that window. The listing reads names it did not
    # create, and there the blanket answer is the point: an entry the kernel
    # will not stat is neither a link to honour nor a restorable album, so the
    # page answers instead of 500ing. Do not "fix" either one to match the
    # other.
    if dest.is_symlink():
        return
    write_trash_origin(origins_dir, dest.name, origin=origin, moved=moved)


def trash_folder(
    folder: Path, *, trash_dir: Path, origins_dir: Path, protected: ProtectedTrees
) -> Path:
    """Move an orphan husk folder (no tracked items) wholesale into Trash.

    Reversible: ``shutil.move`` relocates the whole directory under ``trash_dir`` to
    a collision-free name and returns the destination. No DB interaction — these
    folders hold only art/sidecars, never library items (unlike
    :func:`trash_album`). Caller owns guard/selection (``find_orphan_folders``).

    The origin record matters most here — a folder with no audio cannot be
    imported, so a husk with no record has no exit from Trash but deletion — so
    this mover refuses (``TrashOriginsStoreUnusableError``) rather than move one
    it could not record. Also raises
    :class:`~app.beets.protected.ProtectedTreeError` when the husk is or holds an
    app directory by inode.
    """
    require_usable_store(origins_dir)
    refuse_protected_tree(folder, protected, action="moved")
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_trash_dest(trash_dir, origins_dir, folder.name)
    origin = os.path.abspath(str(folder))
    shutil.move(str(folder), str(dest))
    # The husk's ONLY exit from Trash. An audio-free folder cannot be imported,
    # so before this record it could be permanently deleted and nothing else;
    # with it, Restore moves it straight back where the sweep took it from.
    _record_origin(origins_dir, dest, origin=origin, moved="folder")
    return dest


#: The copy the EXDEV arm creates in the container. ``O_EXCL`` refuses a name
#: that is already taken instead of writing into it, ``O_NOFOLLOW`` refuses a
#: symlink planted at it.
_COPY_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


def _st_ident(st: os.stat_result) -> tuple[int, int]:
    """``(st_dev, st_ino)`` — the twin of ``trash_manage._ident``."""
    return (st.st_dev, st.st_ino)


def _is_the_staged_entry(current: os.stat_result, st: os.stat_result) -> bool:
    """Whether a re-``lstat`` still describes the entry the caller staged.

    Four fields, because an identity is not unique over TIME: a filesystem that
    allocates inodes from a bitmap hands a freed number out again, so a name
    unlinked and re-created can match ``(st_dev, st_ino)``. Measured 2026-09-13
    on ubuntu-latest: a regular file written at a symlink's just-freed number
    matched, and the unlink took it.

    ``S_IFMT`` comes from the newcomer's OWN mode rather than from the number
    it was handed, so a regular file at a symlink's number reads as a regular
    file and this refuses it. ``st_size`` and ``st_mtime_ns`` also catch a
    rewrite IN PLACE, which keeps the inode; refusing there leaves the source
    alone and the copy in Trash, the direction that loses nothing.

    Measured 2026-09-13: all four survive the real copy path unchanged on tmpfs
    and btrfs, for a regular file and for a symlink, so a legitimate unlink
    still passes. ``st_atime_ns`` is out — the copy READS the source, and a
    mount that records reads would move it.
    """
    return (
        _st_ident(current) == _st_ident(st)
        and stat.S_IFMT(current.st_mode) == stat.S_IFMT(st.st_mode)
        and current.st_size == st.st_size
        and current.st_mtime_ns == st.st_mtime_ns
    )


def _refuse_a_non_file(name: str, st: os.stat_result) -> None:
    """Refuse anything but a regular file or a symlink, from a staged ``lstat``."""
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
        raise OSError(errno.EINVAL, "not a regular file or a symlink", name)


def _refuse_a_non_bare_name(name: str) -> None:
    """Refuse a name that is not a bare entry of the source directory.

    What the anchoring rests on, so it is a syscall-free check rather than a
    sentence in a docstring. POSIX ignores a ``dir_fd`` for an ABSOLUTE name:
    ``os.rename(name, name, src_dir_fd=, dst_dir_fd=)`` renamed the file onto
    itself and REPORTED success — measured, ``moved`` counted it and the origin
    record was written for a container that got nothing. ``../library.db``
    un-anchored both ends and left the file loose at the Trash ROOT. A separator
    anchors only the FIRST component: ``os.stat("sub/f", dir_fd=,
    follow_symlinks=False)`` reached a file outside the descriptor through a
    symlinked ``sub``.
    """
    if not name or name in (".", "..") or "/" in name or os.sep in name:
        raise OSError(errno.EINVAL, "not a bare entry name", name)


def _publish_then_unlink(
    name: str, *, src_dir_fd: int, dst_dir_fd: int, st: os.stat_result
) -> None:
    """Make the container's new entry durable, then drop the original.

    ``fsync`` on the CONTAINER's descriptor first, because until that returns
    the only entry naming those bytes on disk may still be the one about to go.

    There is no unlink-by-fd, so the source is re-lstat'd through ``src_dir_fd``
    immediately before it and compared field by field with what the caller
    staged (:func:`_is_the_staged_entry`: identity, ``S_IFMT``, size, mtime): a
    file that arrived after the copy never reached Trash, and unlinking it would
    destroy it (measured). It is left alone instead — the copy stays in Trash,
    the newcomer stays on disk — with one line, because the pair then reads as a
    duplicate. The window is not closed, only narrowed to the two syscalls; an
    entry that differs in none of the four fields still passes it.

    A name that is GONE needs no unlink: somebody else removed it and the copy
    in Trash is the end state this was moving towards.

    A filesystem that cannot fsync a DIRECTORY at all is not a failed move —
    FUSE and network mounts, which is exactly why this arm runs — so
    ``fsutil.fsync_dir`` swallows those two errnos, and only for an fd that IS a
    directory: measured, propagating one turned a completed move into a refusal
    with the file in two places, and EINVAL alone would also have swallowed a
    descriptor number a socket had taken over.
    """
    fsync_dir(dst_dir_fd)
    try:
        current = os.stat(name, dir_fd=src_dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not _is_the_staged_entry(current, st):
        logger.warning(
            "%r was replaced after it was copied to Trash, so it was left in place",
            display_path(name),
        )
        return
    os.unlink(name, dir_fd=src_dir_fd)


def _copy_between_fds(name: str, *, src_dir_fd: int, dst_dir_fd: int, st: os.stat_result) -> None:
    """Copy ``name`` from one directory descriptor to the other, then unlink it.

    The EXDEV arm, which the Docker default takes for every move: Trash lives on
    ``/data`` and the library on ``/music``. Every name here is resolved against
    one of the two descriptors, so no component is re-read from a path.

    A symlink is recreated from its own target rather than copied through. A
    regular file is opened ``O_NOFOLLOW`` and its ``fstat`` compared with the
    identity the caller lstat'd: another inode at the name means the source
    changed after the check, and the copy is refused. A rewrite IN PLACE is not
    another inode, so a copy can carry post-check bytes with the pre-check mode
    and mtime — the check says the name still means that file, not that the file
    did not change.

    The source goes LAST, through :func:`_publish_then_unlink`: what that
    ordering covers is a CRASH (the original is still where it was), and what it
    does not is a SWAP in the window, which is why the unlink re-reads the name.
    """
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(name, dir_fd=src_dir_fd), name, dir_fd=dst_dir_fd)
        _publish_then_unlink(name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, st=st)
        return
    src_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_dir_fd)
    try:
        if _st_ident(os.fstat(src_fd)) != _st_ident(st):
            raise OSError(errno.EINVAL, "the file changed after it was checked", name)
        mode = stat.S_IMODE(st.st_mode)
        with os.fdopen(src_fd, "rb", closefd=False) as reader:
            dst_fd = os.open(name, _COPY_CREATE_FLAGS, mode, dir_fd=dst_dir_fd)
            with os.fdopen(dst_fd, "wb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
                # Mode and mtime through the copy's OWN fd: a name would be one
                # more re-resolution, and the pair is what a user reads the file
                # back by.
                os.fchmod(writer.fileno(), mode)
                os.utime(writer.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
    finally:
        os.close(src_fd)
    _publish_then_unlink(name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, st=st)


def _move_between_fds(name: str, *, src_dir_fd: int, dst_dir_fd: int, st: os.stat_result) -> None:
    """Move ``name`` between two directory descriptors, or refuse.

    ``os.rename`` first, and the lstat through ``dst_dir_fd`` after it is what
    decides whether what arrived may stay: it must be the same ``(st_dev,
    st_ino)`` the caller staged AND still a regular file or a symlink, or it is
    renamed back and the call refuses. Both of the measured swaps that reach
    here fail it — a directory renamed onto a guarded name, which used to be
    moved whole (the guard's lstat precedes the move by ~0.1 ms), and a
    different regular file, which used to be accepted with the record naming the
    one that was checked.

    The type is re-asked because an identity is not unique over TIME: ext4 and
    xfs allocate inodes from a bitmap and hand a freed number out again, so a
    match can be a directory that took the number of the file this staged —
    measured, with the clause removed, staging a directory's own identity
    relocated the whole tree into the container.

    EXDEV — a Trash dir on another filesystem — falls back to a copy through the
    same two descriptors.
    """
    try:
        os.rename(name, name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_between_fds(name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, st=st)
        return
    landed = os.stat(name, dir_fd=dst_dir_fd, follow_symlinks=False)
    if _st_ident(landed) == _st_ident(st) and (
        stat.S_ISREG(landed.st_mode) or stat.S_ISLNK(landed.st_mode)
    ):
        return
    # Put back, and the call refuses either way. Suppressed because the source
    # name can be occupied again by then: the entry then stays in the container,
    # where the caller's ``moved == 0`` cleanup meets it.
    with contextlib.suppress(OSError):
        os.rename(name, name, src_dir_fd=dst_dir_fd, dst_dir_fd=src_dir_fd)
    raise OSError(errno.EINVAL, "the file changed after it was checked", name)


def _discard_own_container(name: str, *, fd: int, trash_fd: int) -> None:
    """Remove the container this call created, while it is still that directory.

    Reached when nothing moved. ``rmdir`` alone refused a container holding a
    part-copied file and left a Trash row no origin record names, and
    ``shutil.rmtree`` cannot do it through a descriptor: ``rmtree(".",
    dir_fd=fd)`` answers EINVAL (measured) and ``rmtree(name, dir_fd=trash_fd)``
    re-resolves the name.

    The children go through ``fd``, the directory the ``mkdir`` claimed, and the
    ``rmdir`` runs only while ``name`` still means that same directory —
    ``trash_manage._Remover._directory``'s shape. A DIRECTORY among the children
    is left alone: ``unlink`` refuses one, so a stranger's tree renamed in is
    never removed and the ``rmdir`` then answers ENOTEMPTY.
    """
    claimed = os.fstat(fd)
    # Closed before the unlink loop: an fd scandir DUPS the fd and the dup
    # SHARES its offset, so one iterator left open makes every later enumeration
    # of that fd read [] (measured).
    with os.scandir(fd) as entries:
        children = [entry.name for entry in entries]
    for child in children:
        with contextlib.suppress(OSError):
            os.unlink(child, dir_fd=fd)
    with contextlib.suppress(OSError):
        if _st_ident(os.stat(name, dir_fd=trash_fd, follow_symlinks=False)) == _st_ident(claimed):
            os.rmdir(name, dir_fd=trash_fd)


def _root_refusal_strerror(protected: ProtectedTrees) -> str:
    """Which of ``open_checked_dir``'s three refusals this was, without its path.

    Read off the same ``protected`` the refusal was decided from — the alias and
    the absent identity are both tested BEFORE the open, so this is the cause
    rather than a guess. One strerror for all three used to tell an operator
    whose Trash is bind-mounted onto another MusicDrop directory that something
    had raced (security seat L-5).
    """
    if protected.trash is None:
        return "the Trash directory could not be examined when it was checked"
    if protected.trash_alias is not None:
        return "the Trash directory is the same folder as another MusicDrop directory"
    return "the Trash directory changed after it was checked"


def _open_checked_trash_root(trash_dir: Path, protected: ProtectedTrees) -> int:
    """A descriptor on the Trash ROOT: the directory the layout check examined.

    ``store_layout`` refuses a Trash that IS or CONTAINS the music library and
    permits one strictly INSIDE it (the owner's ruling — deletes become same-disk
    renames). In that layout the Trash's parent is attacker-writable, so opening
    the root by path followed a symlink swapped in after ``checked_store_dirs``:
    measured, the container and the file landed in a directory of the attacker's
    choosing while the checked Trash stayed empty and the origin record named an
    entry that does not exist. ``open_checked_dir`` compares the identity
    ``store_layout._ensure_trash_root`` took by ``fstat`` on the descriptor its
    own anchored walk reached — never a second resolve by name, whose 18 µs
    window a racer won 537 times in 100 876 requests (security seat M-1). The
    descriptor this returns is the one ``trash_manage.empty_all`` enumerates
    through, so a symlinked Trash root is refused by both or by neither.

    This is the mover's ONLY open of the root, and it never creates: the Trash is
    created where its identity is taken (``store_layout._ensure_trash_root``),
    which either returns one or raises. So ``protected.trash is None`` now means
    a set built without that walk — no request path builds one, and it is still
    REFUSED rather than skipped, for the caller this module cannot see. The arm
    that used to create the root here is what let a symlinked intermediate
    component relocate the Trash for good (security seat M-3). A stranger's
    directory that predates the creation is accepted, as it was before: that is
    the attacker owning the Trash's location, which no check here can undo.

    The chained ``ProtectedTreeError``'s own sentence ends "Nothing was removed"
    — the remover's wording, since the message is shared with it
    (``tests/test_trash_api.py`` pins that string); it reaches a mover's log
    through ``__cause__`` only.

    Raises:
        OSError: the Trash is not the directory that was checked. An ``OSError``
            rather than ``ProtectedTreeError`` because both callers already have
            one arm for it, and because the reset endpoint relays ``strerror``
            to keep the configured path off the wire while that exception spells
            it in full.
    """
    try:
        return open_checked_dir(trash_dir, protected)
    except ProtectedTreeError as exc:
        raise OSError(errno.EINVAL, _root_refusal_strerror(protected), str(trash_dir)) from exc


def trash_replaced_files(
    names: Sequence[str],
    *,
    src_dir_fd: int,
    container_name: str,
    origin: Path,
    trash_dir: Path,
    origins_dir: Path,
    protected: ProtectedTrees,
) -> Path:
    """Move loose files the app is about to REPLACE into their own Trash container.

    For a write that would otherwise overwrite or unlink a file a person put
    there by hand — the artist-art writer's ``artist-poster.*`` /
    ``artist-background.*``, the uploaded artist portrait. The files go into one
    collision-free container directly under ``trash_dir`` (a loose file at the
    Trash ROOT that ``Item.from_path`` cannot read is listed by neither half of
    ``trash_manage.list_trashed_albums``; a container directory is listed by
    ``_audio_free_entries`` as a zero-track row, so it has an Empty affordance
    - the page renders no Restore for this shape - and Empty-all counts it).

    ``names`` are bare entry names in the directory ``src_dir_fd`` is open on,
    and that descriptor is the only way the sources are reached: the caller
    opened it, so a component swapped for a symlink afterwards cannot move what
    this reads. BARE is enforced, not assumed — :func:`_refuse_a_non_bare_name`
    refuses an absolute name, ``.``, ``..``, and any separator before a stat is
    taken, because a violation did not fail here: it reported success. Both
    callers pass what a ``scandir`` of that descriptor (or ``Path.name``) gave
    them. Non-empty ``names`` is the caller's job.

    Recorded ``moved="files"``, which is what the listing turns into
    ``restore_mode="by_hand"``: the container is not the folder these files came
    from, so moving it back would put a directory where two files were, and an
    import of art has nothing to import. Restoring them is a hand copy out of
    Trash, and the record is what names the folder to copy them into. The record
    goes by PATH, not through a descriptor: a layout ROW keeps the origins store
    out of the music library, so its parent is not attacker-writable. The Trash
    ROOT has no such row — one strictly inside the library is allowed — so it is
    NOT opened by path: :func:`_open_checked_trash_root` opens the identity the
    layout check examined, and everything below it is a name resolved from that
    descriptor.

    ``protected`` is what carries that identity. The entries themselves need no
    tree guard: each is lstat'd through ``src_dir_fd`` first and anything that is
    not a regular file or a symlink is refused, and :func:`_move_between_fds`
    confirms through the CONTAINER's descriptor what each rename landed.

    Two claims on the container name, because one is not enough:
    ``os.mkdir(dest.name, dir_fd=trash_fd)`` refuses anything that predates it
    (EEXIST), and the ``os.open`` + emptiness probe under it refuse a stranger's
    EMPTY directory renamed onto the name in between — ``rename`` REPLACES an
    empty directory (measured), so the claim alone does not hold the name. A
    non-empty one is somebody's content: refused, and nothing in it is touched.

    Raises:
        TrashOriginsStoreUnusableError: the origin store cannot be used.
        OSError: a name is not a bare entry, the Trash root is not the
            directory that was checked, an entry is not a regular file or a
            symlink, the container name was taken, or a move failed. Whatever
            had already moved keeps its record; a container that got nothing is
            removed again, with anything a part-copied move left in it.
    """
    for name in names:
        _refuse_a_non_bare_name(name)  # ahead of the stats: "sub/f" stats OUTSIDE
    staged = [(name, os.stat(name, dir_fd=src_dir_fd, follow_symlinks=False)) for name in names]
    for name, st in staged:
        _refuse_a_non_file(name, st)
    require_usable_store(origins_dir)
    trash_fd = _open_checked_trash_root(trash_dir, protected)
    try:
        # After the root open, so a refused Trash is not allocated in. The
        # allocator still reads by path and answers a CANDIDATE; what claims it
        # is the ``mkdir`` below, inside the checked directory. ``dest.name`` is
        # a name that descriptor resolves — ``_one_trash_level`` made it a
        # single separator- and NUL-free level.
        dest = _unique_trash_dest(trash_dir, origins_dir, container_name)
        os.mkdir(dest.name, dir_fd=trash_fd)
        # An open that fails leaves the claimed directory behind rather than
        # removing it: with no fd there is no identity to check, and an empty
        # container in Trash is litter where removing a stranger's would not be.
        fd = os.open(dest.name, BELOW_FLAGS, dir_fd=trash_fd)
        try:
            # Closed before anything else touches ``fd`` (the scandir dup shares
            # the offset — see ``_discard_own_container``).
            with os.scandir(fd) as entries:
                stranger = next(entries, None) is not None
            if stranger:
                raise OSError(errno.ENOTEMPTY, "the container name was taken", str(dest))
            moved = 0
            try:
                for name, st in staged:
                    _move_between_fds(name, src_dir_fd=src_dir_fd, dst_dir_fd=fd, st=st)
                    moved += 1
            finally:
                # The record is written for a PARTIAL move too: the files that
                # did land are in Trash whatever the caller does next, and a
                # container with no record reads as "predates origin records"
                # instead of naming the folder it came out of. Nothing moved
                # means nothing to say — and an empty container would sit in the
                # Trash page forever.
                if moved:
                    # ``origin`` is recorded unchecked, and ``moved="files"`` is
                    # what makes that safe: ``trash_origins.move_back_target``
                    # returns None on any record that is not ``moved="folder"``,
                    # before it reaches its lexical containment test, so no path
                    # here ever steers a rename.
                    _record_origin(
                        origins_dir, dest, origin=os.path.abspath(str(origin)), moved="files"
                    )
                else:
                    _discard_own_container(dest.name, fd=fd, trash_fd=trash_fd)
        finally:
            os.close(fd)
    finally:
        os.close(trash_fd)
    return dest


def resolve_trash_dir(settings: Settings, handle: LibraryHandle) -> Path:
    """Where resolved-away copies go: configured ``trash_dir`` or ``<beets_dir>/trash``.

    Empty setting = default under the handle's already-absolute ``beets_dir``
    (sidesteps the cwd-relative gotcha). A configured override is resolved to
    absolute. Synchronous (pathlib I/O must not run on the event loop).

    Resolving is all this does; WHERE the result may sit is
    :mod:`app.beets.store_layout`'s question — at startup, on Save/Apply, and at
    every destructive use site, because ``trash_manage.empty_all`` runs ``rmtree``
    on every unprotected child of whatever comes back. A Trash strictly inside the
    music library is allowed (deletes become same-disk renames); one that IS or
    CONTAINS the library, the beets dir, the database, the origin store or another
    app store is refused.
    """
    if settings.trash_dir:
        return Path(settings.trash_dir).resolve()
    return handle.beets_dir / "trash"


def resolve_trash_origins_dir(settings: Settings, handle: LibraryHandle) -> Path:
    """Where the origin records go: ``trash_origins_dir`` or ``<beets_dir>/trash-origins``.

    Same shape as :func:`resolve_trash_dir` and resolved from the same two
    inputs, so the pair is always read together. Deliberately NOT derived from
    the resolved ``trash_dir``: a configured Trash dir may point anywhere,
    including inside the music library, and a record reachable from ``/music`` is
    the whole thing this store exists to avoid.

    :mod:`app.beets.store_layout` is what holds that to it. It refuses a store
    at, above or inside the music library, at or above the beets data dir, and
    one that is or contains ``trash_dir`` (or sits inside it). Where it is asked: at
    startup, on Validate/Save/Apply, and — because this function calls
    ``Path.resolve()`` on every call, so what the configured string points at can
    change under a running process — at each destructive use site, through
    :func:`app.beets.store_layout.checked_store_dirs`. What is left uncovered is
    the interval between that check and the syscall beside it.
    """
    if settings.trash_origins_dir:
        return Path(settings.trash_origins_dir).resolve()
    return handle.beets_dir / "trash-origins"
