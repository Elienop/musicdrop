"""Faceted Browse over an in-process library cache.

One small ``BrowseRow`` per album (sort keys + nine representative facet
values), built by ONE full scan and kept until ``invalidate_browse_cache()``.
Invalidation is wired into ``app.events.emit.emit_library_changed`` — the same
choke point every mutation path already calls for SSE — so the cache inherits
exactly the staleness signal the frontend trusts. Single-writer assumption
(same as the bank index): this app is the library DB's only writer; out-of-band
edits appear after a restart or an in-app Disk sync (which emits the event).

Facet semantics are unchanged from v1: one representative value per album
("Unknown" when absent) so counts sum to the album total; OR within a facet,
AND across facets.
"""

from __future__ import annotations

import os
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Literal, NamedTuple

from beets import config
from beets.library import Album as BeetsAlbum
from beets.library import Library

from app.beets.library import (
    _album_genre,
    _coerce_int,
    _coerce_optional_str,
    _coerce_str,
    _coerce_year,
    _to_album_cached,
)
from app.models.album import Album
from app.models.browse import BrowseFacets, FacetValue


class BrowseRow(NamedTuple):
    """Sort keys + representative facet values for one album."""

    album_id: int
    artist_key: str
    album_key: str
    # Raw (non-casefolded) album artist, for ``list_albums``' exact,
    # case-sensitive ``?artist=`` filter. ``artist_key`` is its casefold.
    albumartist: str
    added: float
    genre: str
    decade: str
    format: str
    album_type: str
    source: str
    media: str
    country: str
    lyrics: str
    tracks: str
    # Carried so the album-list pages (list_albums / browse_albums / recent_albums)
    # can build their Album models from this cached scan instead of paying a
    # per-row ``album.items()`` query just to recompute track_count + genre.
    # ``genre`` above is the "Unknown"-defaulted FACET value; ``genre_raw`` keeps
    # the nullable album genre so the Album model's genre stays null (not
    # "Unknown") exactly as ``_to_album`` returned it before.
    track_count: int
    genre_raw: str | None


_LOCK = threading.Lock()
_ROWS: dict[str, list[BrowseRow]] = {}  # resolved DB path -> rows


def invalidate_browse_cache() -> None:
    """Drop every cached library (mutation happened / test isolation)."""
    with _LOCK:
        _ROWS.clear()


def _cache_key(lib: Library) -> str:
    return str(Path(os.fsdecode(lib.path)).resolve())


def _facet_str(value: object) -> str:
    return _coerce_optional_str(value) or "Unknown"


def _album_decade(year: int | None) -> str:
    """A year bucketed to its decade label (``"2010s"``); ``None``/0 -> ``"Unknown"``."""
    if not year:
        return "Unknown"
    return f"{(year // 10) * 10}s"


def _album_format(items: list[Any]) -> str:
    """The album's predominant item format (``"FLAC"``); no item format -> ``"Unknown"``.

    ``format`` is a beets item field set at import from the file's MediaFile;
    a mixed-format album takes its most common value (one value per album).
    """
    formats = [f for it in items if (f := _coerce_optional_str(it.get("format"))) is not None]
    if not formats:
        return "Unknown"
    return Counter(formats).most_common(1)[0][0]


def _album_lyrics_bucket(items: list[Any]) -> str:
    """Complete (every track has lyrics) / Partial / Missing (none, or no tracks)."""
    have = sum(1 for it in items if _coerce_str(it.get("lyrics")).strip())
    if not items or have == 0:
        return "Missing"
    return "Complete" if have == len(items) else "Partial"


def _album_tracks_bucket(album: BeetsAlbum, items: list[Any]) -> str:
    """Complete / Incomplete / Unknown vs the matched release's track count.

    Mirrors beets' ``Album.albumtotal`` (the ``missing`` plugin's completeness
    source): expected = ``items[0].tracktotal`` for single-disc / non-per-disc
    numbering, else one ``tracktotal`` per distinct disc — computed from the
    items already in hand so the cache scan issues no extra queries. As-is /
    unmatched imports carry no ``tracktotal`` -> ``Unknown``. Inherited beets
    caveat: an album missing an ENTIRE disc undercounts ``expected`` (no item
    exists to carry that disc's total).
    """
    if not items:
        return "Unknown"
    disctotal = _coerce_int(album.get("disctotal"))
    if disctotal <= 1 or not bool(config["per_disc_numbering"].get(bool)):
        expected = _coerce_int(items[0].get("tracktotal"))
    else:
        seen: set[int] = set()
        expected = 0
        for it in items:
            disc = _coerce_int(it.get("disc"))
            if disc in seen:
                continue
            seen.add(disc)
            expected += _coerce_int(it.get("tracktotal"))
    if expected <= 0:
        return "Unknown"
    return "Complete" if len(items) >= expected else "Incomplete"


def _coerce_added(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]  # beets value is untyped
    except (TypeError, ValueError):
        return 0.0


def _build_row(album: BeetsAlbum) -> BrowseRow:
    items = list(album.items())
    albumartist = _coerce_str(album.albumartist)
    genre_raw = _album_genre(album, items)
    return BrowseRow(
        album_id=int(album.id),
        artist_key=albumartist.casefold(),
        album_key=_coerce_str(album.album).casefold(),
        albumartist=albumartist,
        added=_coerce_added(album.get("added")),
        track_count=len(items),
        genre_raw=genre_raw,
        genre=genre_raw or "Unknown",
        # "80s" means the music's era: original release year, falling back to
        # the (possibly reissue) release year when beets has no original_year.
        decade=_album_decade(_coerce_year(album.get("original_year")) or _coerce_year(album.year)),
        format=_album_format(items),
        album_type=_facet_str(album.get("albumtype")),
        source=_facet_str(album.get("data_source")),
        media=_facet_str(album.get("media")),
        country=_facet_str(album.get("country")),
        lyrics=_album_lyrics_bucket(items),
        tracks=_album_tracks_bucket(album, items),
    )


def _rows(lib: Library) -> list[BrowseRow]:
    """Cached rows for this library, building with ONE full scan on a miss.

    Built under the lock so concurrent browse calls don't race duplicate scans;
    endpoints run in the threadpool, so the lock is required either way.
    """
    key = _cache_key(lib)
    with _LOCK:
        cached = _ROWS.get(key)
        if cached is not None:
            return cached
        rows = [_build_row(album) for album in lib.albums()]
        _ROWS[key] = rows
        return rows


def _facet_values_by_count(counter: Counter[str]) -> list[FacetValue]:
    """Most albums first, then alphabetical; ``"Unknown"`` always sinks last."""

    def key(item: tuple[str, int]) -> tuple[bool, int, str]:
        value, count = item
        return (value == "Unknown", -count, value.casefold())

    return [FacetValue(value=v, count=c) for v, c in sorted(counter.items(), key=key)]


def _facet_values_decades(counter: Counter[str]) -> list[FacetValue]:
    """Newest decade first; ``"Unknown"`` last."""

    def key(item: tuple[str, int]) -> tuple[bool, int]:
        value, _count = item
        start = int(value.rstrip("s")) if value != "Unknown" else 0
        return (value == "Unknown", -start)

    return [FacetValue(value=v, count=c) for v, c in sorted(counter.items(), key=key)]


def browse_facets(lib: Library) -> BrowseFacets:
    """Whole-library facet values + per-value album counts, from the cache.

    Absolute counts (not filter-aware) — a drill-down refinement is deferred.
    """
    counters: dict[str, Counter[str]] = {
        field: Counter()
        for field in (
            "genre",
            "decade",
            "format",
            "album_type",
            "source",
            "media",
            "country",
            "lyrics",
            "tracks",
        )
    }
    for row in _rows(lib):
        for field, counter in counters.items():
            counter[getattr(row, field)] += 1
    return BrowseFacets(
        genres=_facet_values_by_count(counters["genre"]),
        decades=_facet_values_decades(counters["decade"]),
        formats=_facet_values_by_count(counters["format"]),
        album_types=_facet_values_by_count(counters["album_type"]),
        sources=_facet_values_by_count(counters["source"]),
        media=_facet_values_by_count(counters["media"]),
        countries=_facet_values_by_count(counters["country"]),
        lyrics=_facet_values_by_count(counters["lyrics"]),
        tracks=_facet_values_by_count(counters["tracks"]),
    )


def browse_albums(
    lib: Library,
    *,
    genres: list[str],
    decades: list[str],
    formats: list[str],
    album_types: list[str] | None = None,
    sources: list[str] | None = None,
    medias: list[str] | None = None,
    countries: list[str] | None = None,
    lyrics: list[str] | None = None,
    tracks: list[str] | None = None,
    sort: Literal["artist", "added"] = "artist",
    limit: int,
    offset: int,
) -> tuple[list[Album], int]:
    """Albums matching the facet filters (OR within, AND across), sorted + paged.

    Filtering/sorting/paging run entirely over cached rows; only the page slice
    (<= limit albums) is loaded from beets for mapping.
    """
    constraints: list[tuple[str, set[str]]] = [
        ("genre", set(genres)),
        ("decade", set(decades)),
        ("format", set(formats)),
        ("album_type", set(album_types or [])),
        ("source", set(sources or [])),
        ("media", set(medias or [])),
        ("country", set(countries or [])),
        ("lyrics", set(lyrics or [])),
        ("tracks", set(tracks or [])),
    ]
    matched = [
        row
        for row in _rows(lib)
        if all(not values or getattr(row, field) in values for field, values in constraints)
    ]
    if sort == "added":
        matched.sort(key=lambda r: (-r.added, r.artist_key, r.album_key, r.album_id))
    else:
        matched.sort(key=lambda r: (r.artist_key, r.album_key, r.album_id))
    albums: list[Album] = []
    for row in matched[offset : offset + limit]:
        album = lib.get_album(row.album_id)
        # vanished mid-window — defensive, single-writer makes it near-impossible
        if album is not None:
            # track_count + genre come from this row's cache scan — no per-row
            # album.items() query (see _to_album_cached).
            albums.append(_to_album_cached(album, track_count=row.track_count, genre=row.genre_raw))
    return albums, len(matched)
