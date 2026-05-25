"""The beets-adapter boundary.

This is the ONLY module in the codebase that imports beets. Everything beyond
this file works in terms of our own Pydantic models, so beets' untyped surface,
global config singletons, and version quirks stay isolated here.
"""

from beets.library import Album as BeetsAlbum
from beets.library import Library

from app.models.album import Album


def open_library(library_path: str, directory: str | None = None) -> Library:
    """Open a beets library database at ``library_path``.

    ``directory`` is the music root beets indexed; it is optional for read-only
    access but kept for parity with how beets opens a library.
    """
    return Library(library_path, directory=directory)


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


def _album_genre(album: BeetsAlbum) -> str | None:
    """Read album-level genre, falling back to the first track's genre."""
    genre = _coerce_optional_str(album.get("genre"))
    if genre is not None:
        return genre
    for item in album.items():
        item_genre = _coerce_optional_str(item.get("genre"))
        if item_genre is not None:
            return item_genre
    return None


def _to_album(album: BeetsAlbum) -> Album:
    return Album(
        id=int(album.id),
        album_artist=_coerce_str(album.albumartist),
        title=_coerce_str(album.album),
        year=_coerce_year(album.year),
        track_count=len(list(album.items())),
        genre=_album_genre(album),
    )


def list_albums(lib: Library, *, limit: int, offset: int) -> tuple[list[Album], int]:
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
