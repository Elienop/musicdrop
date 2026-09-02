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
instead of re-filing it by path template. Writing that record can never fail a
delete — see ``trash_origins.write_trash_origin``.

Lives in its own module so both features import it without forming the
``import_session -> duplicates -> registry -> import_session`` cycle. Imports
only beets + the base adapter + settings (no registry/duplicates import).
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

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
from app.beets.trash_origins import (
    _NAME_MAX,
    MovedShape,
    origin_recorded,
    write_trash_origin,
)
from app.config import Settings
from app.fsutil import exists


class TrashMoveIncompleteError(Exception):
    """``Album.move`` returned normally but relocated nothing.

    beets answers a source file it cannot find by logging and returning
    (``beets/library/models.py:1178-1192``), so a move that moved NOTHING is
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


def _trash_container_name(album: Any) -> str:
    """A readable, filesystem-safe ``"<albumartist> - <album>"`` container name.

    Path separators (``os.sep`` and a literal ``/`` on any OS) are neutralised so
    the name is a single dir level; both fields empty falls back to ``"album"``
    (``_unique_trash_dest`` handles that too, but keep the intent explicit here).
    """
    artist = _coerce_optional_str(getattr(album, "albumartist", None)) or ""
    title = _coerce_optional_str(getattr(album, "album", None)) or ""
    name = f"{artist} - {title}".strip(" -") if (artist or title) else ""
    for bad in {os.sep, "/"}:
        name = name.replace(bad, "_")
    return name or "album"


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
      album. Moving nothing is correct here and dropping the rows is the point
      (the deliberate cleanup ``trash_album_folder`` spells out in its own ghost
      branch; reached through this path by import Replace and duplicates
      resolve). Allowed through — but only once
      ``require_library_present`` has shown some OTHER album is still on disk,
      because a dropped share with a stray entry on its mountpoint produces this
      exact state for every album at once.
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


def trash_album(lib: Library, album: Any, *, trash_dir: Path, origins_dir: Path) -> str:
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

    Guarded on BOTH sides of the move, because neither half is enough alone.
    beets 2.12's ``Item.move`` silently skips a source file that is not there
    ("If the source file is missing, skip the move", ``log.warning`` then
    ``return`` — ``beets/library/models.py:1178-1192``), so with the share
    unmounted every move no-ops while ``Album.move`` still returns normally, and
    ``Album.remove`` would drop the rows anyway: a Trash path naming an empty
    folder and a library that has forgotten the album.

    * **Before** — :func:`~app.beets.library.require_library_root` raises
      ``LibraryRootUnavailableError`` if the music root is missing, empty or
      unreadable. It runs ahead of the ``mkdir`` as well as the moves, because a
      beets transaction COMMITS on the way out even while unwinding an exception
      (``beets/dbcore/db.py:924-941`` — no rollback branch), so aborting after a
      mutation would not undo it.
    * **After** — the rows are dropped only once the items' stored paths are
      provably under the container. A pre-check is point-in-time: the share can
      drop in the window between it and the first move, and then the check has
      passed on a library that is already gone. See :func:`_require_move_happened`
      for which shortfalls raise (``LibraryRootUnavailableError`` when the root is
      the cause, :class:`TrashMoveIncompleteError` otherwise) and which one is
      tolerated.
    """
    require_library_root(lib)
    trash_dir.mkdir(parents=True, exist_ok=True)
    container = _unique_trash_dest(trash_dir, origins_dir, _trash_container_name(album))
    container.mkdir(parents=True, exist_ok=True)
    # BEFORE the move: ``Album.move`` rewrites every item's stored path, so this
    # is the last moment the album's own folder can be read off the rows.
    pre_move_items = list(album.items())
    source_root = _album_root(lib, pre_move_items) if pre_move_items else ""
    basedir = bytestring_path(str(container))
    album.move(basedir=basedir)  # relocate under the container + prune source dir
    items = list(album.items())
    # POST-CONDITION. ``Album.move`` cannot report a skip: beets logs a missing
    # source and returns (models.py:1178-1192), so "move returned" is not
    # "files moved". The pre-check above closes the window it can see; this
    # closes the one it cannot — the share dropping AFTER the check, and every
    # other cause of a silent skip. Rows are dropped only once the files are
    # provably somewhere else.
    moved = _moved_under(lib, items, container)
    if len(moved) != len(items):
        _require_move_happened(lib, album, container, items=items, moved=moved)
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


def _album_root(lib: Library, items: list[Any]) -> str:
    """The album's on-disk folder: the deepest common dir of its item files.

    Single item -> that file's directory; multi-disc -> the common ancestor of
    the ``Disc N`` subfolders (their parent ``$album`` folder).
    """
    dirs = [os.path.dirname(_abs_path(lib, it.path)) for it in items]
    return dirs[0] if len(dirs) == 1 else os.path.commonpath(dirs)


# Any other album's/singleton's file at or under the folder (both stored path
# forms — see _folder_is_shared). substr (not LIKE) so %/_ in folder names need
# no escaping; byte-exact BLOB compare, same case semantics as the old string
# comparison on Linux. album_id IS NULL = a singleton item — a potential sharer
# too (the old loop's `int(item.album_id or 0)` treated it the same way).
_SHARED_UNDER_SQL = """
SELECT COUNT(*) FROM items
WHERE (album_id IS NULL OR album_id != ?)
  AND (path = ? OR substr(path, 1, ?) = ?
    OR path = ? OR substr(path, 1, ?) = ?)
"""

# Stored paths that byte-prefix matching cannot be trusted to judge: a `..`/`.`
# segment or a doubled slash can NORMALIZE to somewhere else entirely. beets
# writes normalized paths, so these are essentially never present — but a wrong
# "not shared" here trashes a SIBLING album's files, so the rare weird row gets
# the old full normalization treatment instead of being assumed clean.
_WEIRD_PATHS_SQL = """
SELECT path FROM items
WHERE (album_id IS NULL OR album_id != ?)
  AND (instr(path, ?) > 0 OR instr(path, ?) > 0 OR instr(path, ?) > 0
    OR substr(path, 1, 3) = ? OR substr(path, 1, 2) = ?)
"""


def _folder_is_shared(lib: Library, album: Any, album_root: str) -> bool:
    """Whether moving ``album_root`` wholesale would catch files that aren't this
    album's — so the whole-folder trash must NOT be used.

    True when the folder is the library root (or above/outside it), or any OTHER
    album (or singleton) has an item under it. Guards a sibling album from
    becoming collateral.

    Scoped SQL, not a library scan: the old implementation materialized every
    beets ``Item`` in the library (seconds at 75k tracks, and once PER ALBUM
    inside delete-artist — minutes, all under the swap lock). The DB stores
    paths RELATIVE to ``lib.directory`` in the normal case but absolute for
    legacy/outside rows, so the prefix is matched in BOTH forms; only when the
    fast query finds nothing AND pathologically-shaped rows exist (``..``/``.``
    segments, doubled slashes — byte-prefix-unjudgeable) do those few rows get
    the old normalization logic. Errs toward "shared": a false True merely
    downgrades to per-file trash; a false False would trash a sibling's files.
    """
    music_dir = os.path.normpath(_abs_path(lib, lib.directory))
    root = os.path.normpath(album_root)
    if not root or root == music_dir:
        return True
    try:
        if os.path.commonpath([root, music_dir]) != music_dir:
            return True  # not strictly inside the library
    except ValueError:
        return True  # different drives / unrelated paths
    this_id = int(album.id)

    sep = os.fsencode(os.sep)
    abs_root = os.fsencode(root)
    rel_root = os.fsencode(os.path.relpath(root, music_dir))
    abs_prefix = abs_root + sep
    rel_prefix = rel_root + sep
    with lib.transaction() as tx:
        shared = tx.query(
            _SHARED_UNDER_SQL,
            (
                this_id,
                rel_root,
                len(rel_prefix),
                rel_prefix,
                abs_root,
                len(abs_prefix),
                abs_prefix,
            ),
        )[0][0]
        if int(shared) > 0:
            return True
        weird = tx.query(
            _WEIRD_PATHS_SQL,
            (this_id, b"/../", b"//", b"/./", b"../", b"./"),
        )
    root_with_sep = os.path.join(root, "")
    for row in weird:
        # ``os.fsencode``, never ``bytes(...)`` — see ``_sampled_library_dirs``
        # for why a raw ``path`` row can come back as ``str``. Here the TypeError
        # would 500 a delete that should have taken its ordinary answer.
        path = os.path.normpath(_abs_path(lib, os.fsencode(row[0])))
        if path == root or path.startswith(root_with_sep):
            return True
    return False


def _unique_trash_dest(trash_dir: Path, origins_dir: Path, name: str) -> Path:
    """A non-colliding ``trash_dir/<name>`` (append ``(n)`` if it already exists).

    A name counts as taken when EITHER namespace holds it. The origins half is
    what makes the name key safe: an entry deleted outside MusicDrop (a file
    manager, an SMB client, ``docker volume rm``) leaves its record behind, and
    without this test the next album to earn that name would inherit a stale
    origin — which steers a ``rename()`` for the wrong folder. That is the exact
    hazard inode keys were rejected for, and it is strictly worse than losing an
    origin, so it is closed here rather than by a reaper that would have to
    decide whether an empty Trash dir means "empty" or "unmounted".

    The cost is a burnt name: after a manual deletion the record is litter, and
    an album that would have been ``<name>`` becomes ``<name> (1)``. Litter is
    the accepted residual; a wrong restore is not.

    **Every candidate is kept inside ``NAME_MAX``, and the occupancy test is the
    never-raising one.** ``Path.exists()`` does not absorb ENAMETOOLONG
    (``pathlib._IGNORED_ERRNOS`` is ENOENT/ENOTDIR/EBADF/ELOOP — errno 36 is not
    in it), so appending the first ``" (1)"`` — four bytes — turned any name of
    252 to 255 bytes into an ``OSError(36)`` escaping this function on the very
    first iteration. Measured: 251 passed, 252 and 255 raised, both with a real
    entry in the way and with only an orphaned RECORD in the way. The delete
    then 500s with "Files are recoverable in the Trash folder. Retry." while
    nothing has moved and every retry fails identically, and the orphan sweep
    (whose ``except OSError`` is meant for one bad folder) skips such a folder in
    silence. Shortening the HEAD to make room for the suffix is the answer rather
    than refusing or truncating elsewhere: a Trash entry's name is only a
    container, and what a restore reads to put the folder back is the origin
    RECORD, never the name. :func:`~app.fsutil.exists` then answers "free"
    instead of raising for the limits this constant cannot see — a filesystem
    with a smaller ``NAME_MAX`` (eCryptfs stops at 143 bytes), or a Trash path
    close to ``PATH_MAX`` — leaving the failure to the move, which can at least
    name the path. No whole-delete fixture can reach that half — with the
    shortening in place, putting ``dest.exists()`` back leaves every end-to-end
    long-name test green, because once every candidate fits the constant ABOVE
    there is nothing left for it to absorb. It is pinned directly instead, by
    forcing the predicate to raise the errno this paragraph names:
    ``test_a_name_the_KERNEL_refuses_reads_as_free_and_not_as_a_500``.

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
    half and leave the Trash entry named a different string from the one written
    to the origin record's key; dropping trailing codepoints cannot, and that
    holds for a non-UTF-8 folder name too — its undecodable bytes are carried as
    one lone surrogate each, which ``os.fsencode`` puts back as one byte each.
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
    if dest.is_symlink():
        return
    write_trash_origin(origins_dir, dest.name, origin=origin, moved=moved)


def trash_album_folder(lib: Library, album: Any, *, trash_dir: Path, origins_dir: Path) -> str:
    """Relocate the album's ENTIRE folder under ``trash_dir`` and drop it from the
    library. Reversible.

    Unlike :func:`trash_album` (which moves items by path template and leaves
    untracked files behind), this moves the whole folder — audio, cover art, the
    ``.lrc``/``.txt`` lyric sidecars, and any extras — so nothing is orphaned and
    no empty husk lingers. Falls back to the per-item ``trash_album`` when the
    folder is shared with another album, so a sibling is never collateral.
    Caller owns the transaction.

    The whole-folder branch records its origin, so Restore moves the folder
    straight back to it. The shared-folder fallback records what
    :func:`trash_album` records, and the two row-dropping branches below move
    nothing, so neither has an origin to record.

    Raises :class:`~app.beets.library.LibraryRootUnavailableError` when the
    album's folder is missing AND the library's music cannot be found — an
    unmounted share, not a deleted album. The missing-folder branch uses
    :func:`~app.beets.library.require_library_present`, not the cheap root
    predicate, because that branch is the one that drops rows on nothing but an
    absence. See the branch below.
    """
    items = list(album.items())
    if not items:
        # No item rows means no files and no folder, so there is no on-disk
        # state a mount could protect: this arm skips the root guard below by
        # design. Near-unreachable anyway (beets prunes an album when its last
        # item goes), but not free — inside delete_artist's fan-out it can drop
        # such a row and then have a later album abort the run, which is part of
        # why that caller reports partial progress rather than "nothing moved".
        album.remove(delete=False)
        return str(trash_dir)
    album_root = _album_root(lib, items)
    if not os.path.isdir(album_root):
        # The folder is not there — but that reads two ways, and only one of them
        # means the rows should go:
        #
        #   * deleted outside MusicDrop (over SMB, say) while the rest of the
        #     library is present — a genuine ghost. The DB rows are all that is
        #     left, so drop them and stop advertising files that don't exist.
        #   * the share is not mounted — in which case EVERY album's folder is
        #     "missing" and this branch would erase the library one delete at a
        #     time, keeping nothing recoverable in Trash (nothing moved) while
        #     losing everything library.db held: added dates, play counts, lyrics
        #     flags, flex fields.
        #
        # The root tells them apart, so check it here, at the decision moment —
        # the same per-removal re-check disk sync makes before treating a missing
        # file as a deletion. Raising before ``remove`` is what makes it safe: a
        # beets transaction commits on the way out even while unwinding an
        # exception (``beets/dbcore/db.py:924-941``).
        #
        # The STRONGER predicate, not the shared default: "the root has an entry"
        # is satisfied by a ``.stfolder``/``lost+found``/empty leftover dir on a
        # local mountpoint whose share has dropped, and this branch would then
        # read every album in the library as a ghost and erase it one delete at a
        # time. Disk sync keeps the cheap O(1) default on purpose; the delete
        # path can afford a handful of stats to be sure.
        require_library_present(lib)
        album.remove(delete=False)
        return str(trash_dir)
    if _folder_is_shared(lib, album, album_root):
        return trash_album(lib, album, trash_dir=trash_dir, origins_dir=origins_dir)
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_trash_dest(
        trash_dir, origins_dir, os.path.basename(os.path.normpath(album_root))
    )
    shutil.move(album_root, str(dest))
    # The folder moved whole, so ``dest`` maps 1:1 back onto ``album_root`` and a
    # true restore is a single move. Written after the move (the destination is
    # in Trash, so a failure here cannot leave anything in the music library) and
    # before the rows are dropped, so the record exists from the moment the
    # physical fact it describes is true.
    _record_origin(origins_dir, dest, origin=album_root, moved="folder")
    album.remove(delete=False)  # drop DB rows; files now live under Trash
    return str(dest)


def trash_folder(folder: Path, *, trash_dir: Path, origins_dir: Path) -> Path:
    """Move an orphan husk folder (no tracked items) wholesale into Trash.

    Reversible: ``shutil.move`` relocates the whole directory under ``trash_dir`` to
    a collision-free name and returns the destination. No DB interaction — these
    folders hold only art/sidecars, never library items (unlike
    :func:`trash_album_folder`). Caller owns guard/selection (``find_orphan_folders``).

    The origin record matters most here: a folder with no audio cannot be
    imported, so before it these husks had no exit from Trash except permanent
    deletion.
    """
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_trash_dest(trash_dir, origins_dir, folder.name)
    origin = os.path.abspath(str(folder))
    shutil.move(str(folder), str(dest))
    # The husk's ONLY exit from Trash. An audio-free folder cannot be imported,
    # so before this record it could be permanently deleted and nothing else;
    # with it, Restore moves it straight back where the sweep took it from.
    _record_origin(origins_dir, dest, origin=origin, moved="folder")
    return dest


def resolve_trash_dir(settings: Settings, handle: LibraryHandle) -> Path:
    """Where resolved-away copies go: configured ``trash_dir`` or ``<beets_dir>/trash``.

    Empty setting = default under the handle's already-absolute ``beets_dir``
    (sidesteps the cwd-relative gotcha). A configured override is resolved to
    absolute. Synchronous (pathlib I/O must not run on the event loop).
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
    """
    if settings.trash_origins_dir:
        return Path(settings.trash_origins_dir).resolve()
    return handle.beets_dir / "trash-origins"
