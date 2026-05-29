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


def resolve_trash_dir(settings: Settings, handle: LibraryHandle) -> Path:
    """Where resolved-away copies go: configured ``trash_dir`` or ``<beets_dir>/trash``.

    Empty setting = default under the handle's already-absolute ``beets_dir``
    (sidesteps the cwd-relative gotcha). A configured override is resolved to
    absolute. Synchronous (pathlib I/O must not run on the event loop).
    """
    if settings.trash_dir:
        return Path(settings.trash_dir).resolve()
    return handle.beets_dir / "trash"
