"""Reversible Trash relocation + shared library-album read helpers.

The single low-level "move an album to Trash and drop it from the library"
primitive, shared by the back-door /duplicates resolve op
(``app.beets.duplicates``) and the front-door duplicate-on-import Replace
action (``app.beets.import_session``). Reversible by design: files are
*relocated* (never deleted) and the DB rows dropped with ``delete=False`` —
exactly ``beet dup --move <trash> --remove`` for albums.

Lives in its own module so both features import it without forming the
``import_session -> duplicates -> registry -> import_session`` cycle. Imports
only beets + the base adapter + settings (no registry/duplicates import).
"""

from __future__ import annotations

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
    require_library_root,
)
from app.config import Settings


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


def trash_album(lib: Library, album: Any, *, trash_dir: Path) -> str:
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

    Raises :class:`~app.beets.library.LibraryRootUnavailableError` if the music
    root is missing, empty or unreadable — checked BEFORE anything is created or
    moved. beets 2.12's ``Item.move`` silently skips a source file that is not
    there ("If the source file is missing, skip the move", ``log.warning`` then
    ``return`` — ``beets/library/models.py:1178-1192``), so with the share
    unmounted every move would no-op and ``Album.remove`` below would still drop
    the rows: a Trash path pointing at an empty folder and a library that has
    forgotten the album. The guard has to precede the ``mkdir`` as well as the
    moves, because a beets transaction COMMITS on the way out even while
    unwinding an exception (``beets/dbcore/db.py:924-941`` — no rollback branch),
    so aborting after a mutation would not undo it.
    """
    require_library_root(lib)
    trash_dir.mkdir(parents=True, exist_ok=True)
    container = _unique_trash_dest(trash_dir, _trash_container_name(album))
    container.mkdir(parents=True, exist_ok=True)
    basedir = bytestring_path(str(container))
    album.move(basedir=basedir)  # relocate under the container + prune source dir
    items = list(album.items())
    trash_path = os.path.dirname(_abs_path(lib, items[0].path)) if items else str(container)
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
        path = os.path.normpath(_abs_path(lib, bytes(row[0])))
        if path == root or path.startswith(root_with_sep):
            return True
    return False


def _unique_trash_dest(trash_dir: Path, name: str) -> Path:
    """A non-colliding ``trash_dir/<name>`` (append ``(n)`` if it already exists)."""
    base = name or "album"
    dest = trash_dir / base
    counter = 1
    while dest.exists():
        dest = trash_dir / f"{base} ({counter})"
        counter += 1
    return dest


def trash_album_folder(lib: Library, album: Any, *, trash_dir: Path) -> str:
    """Relocate the album's ENTIRE folder under ``trash_dir`` and drop it from the
    library. Reversible.

    Unlike :func:`trash_album` (which moves items by path template and leaves
    untracked files behind), this moves the whole folder — audio, cover art, the
    ``.lrc``/``.txt`` lyric sidecars, and any extras — so nothing is orphaned and
    no empty husk lingers. Falls back to the per-item ``trash_album`` when the
    folder is shared with another album, so a sibling is never collateral.
    Caller owns the transaction.

    Raises :class:`~app.beets.library.LibraryRootUnavailableError` when the
    album's folder is missing AND the music root itself is unavailable — an
    unmounted share, not a deleted album. See the branch below.
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
        require_library_root(lib)
        album.remove(delete=False)
        return str(trash_dir)
    if _folder_is_shared(lib, album, album_root):
        return trash_album(lib, album, trash_dir=trash_dir)
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_trash_dest(trash_dir, os.path.basename(os.path.normpath(album_root)))
    shutil.move(album_root, str(dest))
    album.remove(delete=False)  # drop DB rows; files now live under Trash
    return str(dest)


def trash_folder(folder: Path, *, trash_dir: Path) -> Path:
    """Move an orphan husk folder (no tracked items) wholesale into Trash.

    Reversible: ``shutil.move`` relocates the whole directory under ``trash_dir`` to
    a collision-free name and returns the destination. No DB interaction — these
    folders hold only art/sidecars, never library items (unlike
    :func:`trash_album_folder`). Caller owns guard/selection (``find_orphan_folders``).
    """
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_trash_dest(trash_dir, folder.name)
    shutil.move(str(folder), str(dest))
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
