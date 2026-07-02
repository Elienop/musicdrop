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

from app.beets.library import LibraryHandle, _abs_path, _coerce_int, _coerce_optional_str
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


def trash_album(lib: Library, album: Any, *, trash_dir: Path) -> str:
    """Relocate one album's files under ``trash_dir`` and drop it from the library.

    Reversible: ``Album.move(basedir=trash)`` relocates the files by path
    template (and prunes the vacated source dir), then
    ``Album.remove(delete=False)`` drops the DB rows while leaving the files in
    Trash. Returns the album's new Trash folder. Caller controls the
    transaction (so a batch can be atomic).
    """
    trash_dir.mkdir(parents=True, exist_ok=True)
    basedir = bytestring_path(str(trash_dir))
    album.move(basedir=basedir)  # relocate under Trash + prune source dir
    items = list(album.items())
    trash_path = os.path.dirname(_abs_path(lib, items[0].path)) if items else str(trash_dir)
    album.remove(delete=False)  # drop DB rows; files stay in Trash
    return trash_path


def _album_root(lib: Library, items: list[Any]) -> str:
    """The album's on-disk folder: the deepest common dir of its item files.

    Single item -> that file's directory; multi-disc -> the common ancestor of
    the ``Disc N`` subfolders (their parent ``$album`` folder).
    """
    dirs = [os.path.dirname(_abs_path(lib, it.path)) for it in items]
    return dirs[0] if len(dirs) == 1 else os.path.commonpath(dirs)


def _folder_is_shared(lib: Library, album: Any, album_root: str) -> bool:
    """Whether moving ``album_root`` wholesale would catch files that aren't this
    album's — so the whole-folder trash must NOT be used.

    True when the folder is the library root (or above/outside it), or any OTHER
    album has an item under it. Guards a sibling album from becoming collateral.
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
    root_with_sep = os.path.join(root, "")
    for item in lib.items():
        if int(item.album_id or 0) == this_id:
            continue
        path = os.path.normpath(_abs_path(lib, item.path))
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
    """
    items = list(album.items())
    if not items:
        album.remove(delete=False)
        return str(trash_dir)
    album_root = _album_root(lib, items)
    if not os.path.isdir(album_root):
        # Ghost album: the folder was deleted outside MusicDrop (the DB rows are
        # all that's left). Nothing to relocate — just drop the rows so the
        # library stops advertising files that don't exist. beets 2.12 would
        # silently skip the per-item moves anyway (missing sources).
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
