"""The beets-adapter boundary.

This is the ONLY module in the codebase that imports beets. Everything beyond
this file works in terms of our own Pydantic models, so beets' untyped surface,
global config singletons, and version quirks stay isolated here.
"""

import os
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from beets.dbcore.query import MatchQuery, ParsingError
from beets.library import Album as BeetsAlbum
from beets.library import Library
from mediafile import MediaFile

from app.beets.release_identity import release_identity
from app.models.album import Album, AlbumDetail, Track
from app.models.artist import Artist
from app.models.search import SearchEntity, SearchResults, SearchTrack, TypedSearchPage

# Allowlist of cover-art extensions we serve. `.svg` is deliberately excluded:
# serving user-controlled SVG (even via <img>) is an XSS footgun, not worth it.
_EXTENSION_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}


@dataclass
class LibraryHandle:
    """Public handle for the opened beets library.

    Callers outside this module annotate with ``LibraryHandle`` so they never
    need to import beets themselves, keeping the adapter the sole beets
    importer (CLAUDE.md rule 3). The handle bundles the opened ``Library`` with
    the metadata the read-only Config view needs: the path of the user-owned
    ``config.yaml``, when ``setup_beets()`` ran, and the file's mtime at load —
    used to flag "restart required" when the file changes on disk.
    """

    lib: Library
    beets_dir: Path
    config_path: Path
    loaded_at: datetime
    file_mtime_at_load: float  # raw stat.st_mtime for direct comparison


def open_library(library_path: str, directory: str | None = None) -> Library:
    """Open a beets library database at ``library_path``.

    ``directory`` is the music root beets indexed. beets 2.12's ``Library`` reads
    the configured path formats + replacements from the global config itself (the
    ``path_formats``/``replacements`` constructor kwargs were removed), so an
    import places and names files exactly as ``beet import`` would — provided the
    config is loaded first (``setup_beets`` does).
    """
    return Library(library_path, directory=directory)


def close_library(lib: Library) -> None:
    """Close the beets library's underlying SQLite connection."""
    lib._close()


def library_paths_context(handle: LibraryHandle) -> AbstractContextManager[Any]:
    """Bind beets' path expansion to this library for the calling thread.

    beets 2.11 stores DB paths relative to the library directory and re-expands
    them through a ContextVar worker threads do not inherit; job runners bind
    this around their whole sweep so reads AND file writes resolve real paths
    off the main thread. The version quirk stays behind the adapter (rule 3).
    """
    ctx: AbstractContextManager[Any] = handle.lib.music_dir_context()
    return ctx


def album_exists(handle: LibraryHandle, album_id: int) -> bool:
    """Whether ``album_id`` is in the library. A scalar read; no path expansion."""
    return handle.lib.get_album(album_id) is not None


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


def _coerce_int(value: object) -> int:
    try:
        number: int = int(value)  # type: ignore[call-overload]  # beets value is untyped
    except (TypeError, ValueError):
        return 0
    return number


def _coerce_duration(value: object) -> float | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]  # beets value is untyped
    except (TypeError, ValueError):
        return None
    return seconds or None


def _album_fields(album: BeetsAlbum, items: list[Any]) -> dict[str, Any]:
    """Map a beets album + its items to the shared ``Album`` field set.

    Factored out so ``_to_album`` and ``get_album_detail`` build the album
    portion from one source of truth instead of duplicating the mapping.
    """
    return {
        "id": int(album.id),
        "album_artist": _coerce_str(album.albumartist),
        "title": _coerce_str(album.album),
        "year": _coerce_year(album.year),
        "track_count": len(items),
        "genre": _album_genre(album, items),
        "mb_albumid": _coerce_optional_str(album.mb_albumid),
    }


def _to_album(album: BeetsAlbum) -> Album:
    items = list(album.items())
    return Album(**_album_fields(album, items))


def _to_track(item: Any) -> Track:
    return Track(
        id=int(item.id),
        title=_coerce_str(item.title),
        track=_coerce_int(item.track),
        disc=_coerce_int(item.disc),
        duration_seconds=_coerce_duration(item.length),
        artist=_coerce_str(item.artist),
        mb_trackid=_coerce_optional_str(item.mb_trackid),
        has_lyrics=bool(item.lyrics),
        format=_coerce_optional_str(item.format),
    )


def get_album_detail(lib: Library, album_id: int) -> AlbumDetail | None:
    """Return an album with its tracklist, or ``None`` when the album is missing.

    Tracks are sorted by ``(disc, track)`` so the tracklist reads in play order.
    """
    album = lib.get_album(album_id)
    if album is None:
        return None
    items = list(album.items())
    tracks = sorted(
        (_to_track(item) for item in items),
        key=lambda t: (t.disc, t.track),
    )
    return AlbumDetail(
        **_album_fields(album, items),
        tracks=tracks,
        release=release_identity(album, album.mb_albumid),
    )


def list_artists(lib: Library) -> list[Artist]:
    """Return the artist roster: one entry per distinct ``albumartist``.

    beets has no first-class artist entity, so we derive it by grouping albums
    on ``albumartist`` and counting distinct albums per artist. Sorted by name
    (case-insensitive) for a stable, readable roster.
    """
    counts: dict[str, int] = {}
    for album in lib.albums():
        name = _coerce_str(album.albumartist)
        # _coerce_str does not strip, so a null/whitespace albumartist would
        # emit a blank, nameless card; drop those albums from the roster.
        if not name.strip():
            continue
        counts[name] = counts.get(name, 0) + 1
    artists = [Artist(name=name, album_count=count) for name, count in counts.items()]
    artists.sort(key=lambda a: a.name.casefold())
    return artists


def get_artist_mbid(lib: Library, name: str) -> str | None:
    """Return the MusicBrainz artist MBID for an albumartist name, or None.

    Reads ``mb_albumartistid`` off the artist's albums. Uses a ``MatchQuery``
    (parameterized ``albumartist = ?``) rather than a string-interpolated query,
    so quotes/colons/metacharacters in the name cannot break it. Returns the
    first value that parses as a UUID (rejecting an empty field or junk like
    ``"520"``); ``None`` if the artist is absent or has no valid MBID.
    """
    for album in lib.albums(MatchQuery("albumartist", name)):
        raw = _coerce_str(album.mb_albumartistid)
        try:
            uuid.UUID(raw)
        except ValueError:
            continue
        return raw
    return None


def _to_search_track(item: Any) -> SearchTrack:
    return SearchTrack(
        id=int(item.id),
        title=_coerce_str(item.title),
        artist=_coerce_str(item.artist),
        album=_coerce_str(item.album),
        # Singletons (tracks beets has not grouped into an album) carry a falsy
        # album_id; surface them as None so the FE knows there is no album page.
        album_id=int(item.album_id) if item.album_id else None,
        duration_seconds=_coerce_duration(item.length),
    )


def search(lib: Library, *, query: str, limit: int) -> SearchResults:
    """Search the library across tracks, albums, and artists for a free-text term.

    ``query`` is handed to beets' query parser for tracks and albums (matching
    substrings across fields); artists are filtered with a case-insensitive
    substring match on the derived roster, since beets has no artist entity.

    A blank/whitespace query short-circuits to empty results with no beets call.
    Each beets query is wrapped so a malformed term (parse error) degrades to no
    results for that entity instead of surfacing as a 500.
    """
    if not query.strip():
        return SearchResults(
            artists=[], albums=[], tracks=[], artist_total=0, album_total=0, track_total=0
        )

    # Tracks: beets free-text query. A malformed query raises ParsingError
    # (an InvalidQueryError/ValueError subclass) from the parser; catch it so a
    # bad term yields no tracks rather than a 500.
    try:
        all_items = list(lib.items(query))
    except ParsingError:
        all_items = []
    track_total = len(all_items)
    tracks = [_to_search_track(item) for item in all_items[:limit]]

    # Albums: same free-text query, mapped through the shared _to_album.
    try:
        all_album_matches = list(lib.albums(query))
    except ParsingError:
        all_album_matches = []
    album_total = len(all_album_matches)
    albums = [_to_album(album) for album in all_album_matches[:limit]]

    # Artists: no beets query entity, so filter the derived roster by a
    # case-insensitive substring match on the name.
    needle = query.casefold()
    artist_matches = [a for a in list_artists(lib) if needle in a.name.casefold()]
    artist_total = len(artist_matches)
    artists = artist_matches[:limit]

    return SearchResults(
        artists=artists,
        albums=albums,
        tracks=tracks,
        artist_total=artist_total,
        album_total=album_total,
        track_total=track_total,
    )


def search_typed(
    lib: Library, *, query: str, entity: SearchEntity, limit: int, offset: int
) -> TypedSearchPage:
    """One entity of the free-text search, paged — backs /api/search?type=…

    Per-entity semantics are identical to ``search`` (beets' query parser for
    tracks/albums with the same ParsingError guard; case-insensitive substring
    over the derived roster for artists) and so is the ordering (beets' default
    query sort / the name-sorted roster), so the typed "View all" page lines up
    with the sectioned preview. ``total`` is the full match count BEFORE the
    ``offset:offset+limit`` slice. A blank/whitespace query short-circuits to
    an empty page with no beets call.
    """
    artists: list[Artist] = []
    albums: list[Album] = []
    tracks: list[SearchTrack] = []
    total = 0
    if query.strip():
        if entity == "tracks":
            try:
                all_items = list(lib.items(query))
            except ParsingError:
                all_items = []
            total = len(all_items)
            tracks = [_to_search_track(item) for item in all_items[offset : offset + limit]]
        elif entity == "albums":
            try:
                all_album_matches = list(lib.albums(query))
            except ParsingError:
                all_album_matches = []
            total = len(all_album_matches)
            albums = [_to_album(a) for a in all_album_matches[offset : offset + limit]]
        else:
            needle = query.casefold()
            matches = [a for a in list_artists(lib) if needle in a.name.casefold()]
            total = len(matches)
            artists = matches[offset : offset + limit]
    return TypedSearchPage(
        type=entity,
        artists=artists,
        albums=albums,
        tracks=tracks,
        total=total,
        limit=limit,
        offset=offset,
    )


def list_albums(
    lib: Library, *, limit: int, offset: int, artist: str | None = None
) -> tuple[list[Album], int]:
    """Return a page of albums mapped to our Pydantic model plus the total count.

    Albums are sorted stably by album artist then album title (case-insensitive),
    so pagination is deterministic regardless of beets' default sort. When
    ``artist`` is set, albums are filtered to that exact ``albumartist`` BEFORE
    paginating, so ``total`` reflects the filtered count.

    Sorting/filtering/paging run over the shared in-memory ``BrowseRow`` cache
    (ONE full scan, invalidated on every ``emit_library_changed``); only the
    page's albums (<= ``limit``) are loaded from beets, not the whole library.
    """
    # Imported lazily: browse.py imports helpers from this module at import
    # time, so a top-level import back would form a cycle.
    from app.beets.browse import _rows

    rows = sorted(_rows(lib), key=lambda r: (r.artist_key, r.album_key, r.album_id))
    if artist is not None:
        rows = [r for r in rows if r.albumartist == artist]
    total = len(rows)
    albums: list[Album] = []
    for row in rows[offset : offset + limit]:
        album = lib.get_album(row.album_id)
        # Vanished between cache build and load — skip defensively; the cache
        # invalidates on every mutation, so this is belt-and-suspenders.
        if album is not None:
            albums.append(_to_album(album))
    return albums, total


def _abs_path(lib: Library, stored: bytes) -> str:
    """Resolve a beets-stored file path to absolute.

    The beets model API returns paths (``item.path``, ``artpath``) already
    resolved to absolute, so on the normal flow the input passes through
    unchanged. The underlying DB rows are stored relative to ``lib.directory``
    (observed with in-place imports); this guard is defense-in-depth for any
    code that reads a raw DB row directly, joining against ``lib.directory``
    only when the path is relative.
    """
    path = os.fsdecode(stored)
    if os.path.isabs(path):
        return path
    return os.path.join(os.fsdecode(lib.directory), path)


def _cover_from_artpath(lib: Library, album: BeetsAlbum) -> tuple[bytes, str] | None:
    raw_path = album.get("artpath")
    if not raw_path:
        return None
    path = _abs_path(lib, raw_path)
    if not os.path.isfile(path):
        return None
    mime = _EXTENSION_MIME.get(os.path.splitext(path)[1].lower())
    if mime is None:
        return None
    with open(path, "rb") as fh:
        return fh.read(), mime


def _cover_from_embedded(lib: Library, album: BeetsAlbum) -> tuple[bytes, str] | None:
    items = list(album.items())
    if not items:
        return None
    track_path = _abs_path(lib, items[0].path)
    if not os.path.isfile(track_path):
        return None
    images = MediaFile(track_path).images
    if not images:
        return None
    image = images[0]
    # Intentional fail-safe: if the embedded image declares no mime, octet-stream
    # makes the browser <img> refuse it, cleanly triggering the FE onError
    # placeholder (serve-or-degrade) rather than rendering garbage.
    mime = _coerce_optional_str(image.mime_type) or "application/octet-stream"
    return bytes(image.data), mime


def get_album_cover(lib: Library, album_id: int) -> tuple[bytes, str] | None:
    """Return ``(image_bytes, mime_type)`` for an album's cover art, or ``None``.

    Resolution order: the album's ``artpath`` file if present, otherwise the
    embedded art on the album's first track. ``None`` when the album is missing
    or no art can be found.
    """
    album = lib.get_album(album_id)
    if album is None:
        return None
    return _cover_from_artpath(lib, album) or _cover_from_embedded(lib, album)
