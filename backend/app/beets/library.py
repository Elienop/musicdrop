"""The beets-adapter boundary.

This is the ONLY module in the codebase that imports beets. Everything beyond
this file works in terms of our own Pydantic models, so beets' untyped surface,
global config singletons, and version quirks stay isolated here.
"""

import os
from typing import Any

from beets.library import Album as BeetsAlbum
from beets.library import Library
from mediafile import MediaFile

from app.models.album import Album

_EXTENSION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Public handle type for the opened beets library. Callers outside this module
# annotate with LibraryHandle so they never need to import beets themselves,
# keeping the adapter the sole beets importer (CLAUDE.md rule 3).
LibraryHandle = Library


def open_library(library_path: str, directory: str | None = None) -> LibraryHandle:
    """Open a beets library database at ``library_path``.

    ``directory`` is the music root beets indexed; it is optional for read-only
    access but kept for parity with how beets opens a library.
    """
    return Library(library_path, directory=directory)


def close_library(lib: LibraryHandle) -> None:
    """Close the beets library's underlying SQLite connection."""
    lib._close()


def _coerce_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_year(value: object) -> int | None:
    try:
        year = int(value)  # type: ignore[call-overload]  # beets value is untyped
    except (TypeError, ValueError):
        return None
    return year or None


def _album_genre(album: BeetsAlbum, items: list[Any]) -> str | None:
    """Read album-level genre, falling back to the album's tracks.

    Heuristic: the first track with a non-empty genre wins (not a mode/majority
    vote). Items are passed in so we don't re-fetch them from the database.
    """
    genre = _coerce_optional_str(album.get("genre"))
    if genre is not None:
        return genre
    for item in items:
        item_genre = _coerce_optional_str(item.get("genre"))
        if item_genre is not None:
            return item_genre
    return None


def _to_album(album: BeetsAlbum) -> Album:
    items = list(album.items())
    return Album(
        id=int(album.id),
        album_artist=_coerce_str(album.albumartist),
        title=_coerce_str(album.album),
        year=_coerce_year(album.year),
        track_count=len(items),
        genre=_album_genre(album, items),
    )


def list_albums(lib: LibraryHandle, *, limit: int, offset: int) -> tuple[list[Album], int]:
    """Return a page of albums mapped to our Pydantic model plus the total count.

    Albums are sorted stably by album artist then album title (case-insensitive),
    so pagination is deterministic regardless of beets' default sort.
    """
    all_albums = sorted(
        lib.albums(),
        key=lambda a: (
            _coerce_str(a.albumartist).casefold(),
            _coerce_str(a.album).casefold(),
            int(a.id),
        ),
    )
    total = len(all_albums)
    page = all_albums[offset : offset + limit]
    return [_to_album(a) for a in page], total


def _cover_from_artpath(album: BeetsAlbum) -> tuple[bytes, str] | None:
    raw_path = album.get("artpath")
    if not raw_path:
        return None
    path = os.fsdecode(raw_path)
    if not os.path.isfile(path):
        return None
    mime = _EXTENSION_MIME.get(os.path.splitext(path)[1].lower())
    if mime is None:
        return None
    with open(path, "rb") as fh:
        return fh.read(), mime


def _cover_from_embedded(album: BeetsAlbum) -> tuple[bytes, str] | None:
    items = list(album.items())
    if not items:
        return None
    track_path = os.fsdecode(items[0].path)
    if not os.path.isfile(track_path):
        return None
    images = MediaFile(track_path).images
    if not images:
        return None
    image = images[0]
    mime = _coerce_optional_str(image.mime_type) or "application/octet-stream"
    return bytes(image.data), mime


def get_album_cover(lib: LibraryHandle, album_id: int) -> tuple[bytes, str] | None:
    """Return ``(image_bytes, mime_type)`` for an album's cover art, or ``None``.

    Resolution order: the album's ``artpath`` file if present, otherwise the
    embedded art on the album's first track. ``None`` when the album is missing
    or no art can be found.
    """
    album = lib.get_album(album_id)
    if album is None:
        return None
    return _cover_from_artpath(album) or _cover_from_embedded(album)
