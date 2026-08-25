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
from collections.abc import Iterable
from itertools import groupby
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

from beets import config
from beets.library import Album as BeetsAlbum
from beets.library import Library

from app.beets.library import (
    _coerce_int,
    _coerce_optional_str,
    _coerce_str,
    _coerce_year,
    _genre_join,
    _genre_values,
    _instrumental_value,
    _require_id,
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


# Two locks so invalidation NEVER waits on a scan. The whole-library build is
# still a multi-second job on an HDD (one albums() scan + the aggregate item
# pass), and invalidate_browse_cache() is called synchronously ON THE EVENT LOOP by every
# mutating endpoint (via emit_library_changed / broker.publish_library_changed).
# With a single lock held across the build, one open Browse tab rebuilding the
# cache in a threadpool thread froze the entire asyncio loop the moment any
# mutation emitted — every endpoint, every SSE stream — until the scan finished.
#
# * ``_STATE_LOCK`` — held only for O(1) dict/int work: guards ``_ROWS`` and
#   ``_GENERATION``. Everything acquires it briefly; nothing blocks under it.
# * ``_BUILD_LOCK`` — held across the scan itself, purely so concurrent browse
#   calls on a cold cache don't run duplicate scans.
# Ordering: ``_BUILD_LOCK`` -> ``_STATE_LOCK`` (nested briefly); invalidation
# takes only ``_STATE_LOCK``, so it can never deadlock or wait on a build.
_STATE_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()
_ROWS: dict[str, list[BrowseRow]] = {}  # resolved DB path -> rows
# Bumped on every invalidation. A build snapshots it before scanning and only
# stores its rows if it is UNCHANGED after — a scan the library mutated under
# is discarded (never cached stale), while the requester still gets the rows.
_GENERATION = 0


def invalidate_browse_cache() -> None:
    """Drop every cached library (mutation happened / test isolation).

    O(1) and non-blocking by design: bumps the generation and clears the dict
    under the brief state lock only. Never waits on an in-flight scan — the
    generation bump makes that scan discard its result instead.
    """
    global _GENERATION
    with _STATE_LOCK:
        _GENERATION += 1
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


class _AlbumFacts(NamedTuple):
    """Everything a ``BrowseRow`` needs from an album's tracks.

    Derived once for the WHOLE library by :func:`_collect_facts`, so the row
    build never touches ``album.items()``. The two track totals are both carried
    because choosing between them needs ``disctotal``, an ALBUM field the item
    pass never sees — :func:`_tracks_bucket` picks one.
    """

    track_count: int
    # Predominant item format ("FLAC"); "Unknown" when no track carries one.
    format: str
    # The genres of the FIRST track that has any, for albums with none of their
    # own. A tuple (not a list) because ``_EMPTY_FACTS`` below is module-level
    # and shared by every track-less album.
    genre_fallback: tuple[str, ...]
    # Complete / Partial / Missing.
    lyrics: str
    # The first track's ``tracktotal`` — the expectation for a single-disc album
    # or one numbered straight through.
    first_tracktotal: int
    # One ``tracktotal`` per DISTINCT disc — the expectation under
    # ``per_disc_numbering``.
    per_disc_tracktotal: int


# An album with no tracks at all: the values the old per-album build produced
# from an empty ``items`` list.
_EMPTY_FACTS = _AlbumFacts(
    track_count=0,
    format="Unknown",
    genre_fallback=(),
    lyrics="Missing",
    first_tracktotal=0,
    per_disc_tracktotal=0,
)

# Every character Python's ``str.strip()`` removes — the argument for SQLite's
# two-argument ``TRIM(X, Y)``, which strips any character appearing in ``Y`` and
# is UTF-8 aware, so the whole set travels as one bound parameter. Hardcoded
# rather than derived: recomputing it means testing ~1.1M codepoints at import.
# ``test_stored_whitespace_constant_matches_python`` re-derives it and asserts
# equality, so a future Python that adds a whitespace character fails loudly
# instead of silently mis-bucketing one album.
_PYTHON_WHITESPACE: Final[str] = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)

# ONE pass over every album's tracks. ``genres`` is a real COLUMN in beets 2.13
# (single-valued ``genre`` was dropped from ``Item._fields``, so reading it now
# falls through to the flex table and answers nothing), which is why it is
# selected here rather than fetched from ``item_attributes`` — one fewer full
# scan of that table, and this query stays the single source of track order.
# Raw column values arrive as the delimiter-joined string beets stores, and are
# split by ``library._genre_values`` using beets' own field type.
#
# ``lyrics`` is a column too, but the only question ever asked of it here is
# WHETHER the track has any — so the answer is computed in SQL and the text
# never crosses the boundary (~25 MB of strings decoded into Python objects and
# thrown away per rebuild at 45k tracks, and a rebuild follows every mutation).
#
# Do NOT "simplify" this to ``TRIM(lyrics) != ''``. One-argument TRIM strips
# SPACES ONLY, while the Python test it replaces was ``_coerce_str(lyrics)
# .strip()``, which strips all 29 characters Python calls whitespace: a lyrics
# value of "\n" would flip from Missing to answered and move its album between
# lyrics buckets with the whole suite still green. The ``typeof`` arm is
# load-bearing for the same reason in the other direction — ``_coerce_str`` is
# ``str(value)``, so a non-TEXT value stringifies to something truthy (an
# INTEGER 0 reads as "0") and has to stay answered.
_ITEM_FACTS_SQL = """
    SELECT
        id,
        album_id,
        format,
        CASE
            WHEN lyrics IS NULL THEN 0
            WHEN typeof(lyrics) = 'text' AND TRIM(lyrics, ?) = '' THEN 0
            ELSE 1
        END AS has_lyrics,
        disc,
        tracktotal,
        genres
    FROM items
    WHERE album_id IS NOT NULL
    -- album_id first so each album's rows arrive contiguous for groupby.
    -- Then PLAY order, deliberately: the per-album album.items() build this
    -- replaced inherited beets' user-configurable `sort_item` DISPLAY sort, so
    -- an album's genre fallback / tied format vote / "first" tracktotal could
    -- shift with a display preference. library._album_genre sorts by the same
    -- key so every endpoint gives one answer per album.
    ORDER BY album_id, disc, track, id
"""

_FLEX_SQL = "SELECT entity_id, value FROM item_attributes WHERE key = ?"


def _lyrics_bucket(answered: int, track_count: int) -> str:
    """Complete / Partial / Missing from an album's answered-track tally."""
    if answered == 0:
        return "Missing"
    if answered == track_count:
        return "Complete"
    return "Partial"


def _album_facts(rows: Iterable[Any], instrumental: set[int]) -> _AlbumFacts:
    """Track facts for ONE album's rows (raw-SQL rows, play order)."""
    track_count = 0
    formats: list[str] = []
    genre_fallback: tuple[str, ...] = ()
    answered = 0
    first_tracktotal = 0
    per_disc_tracktotal = 0
    seen_discs: set[int] = set()
    for raw_id, _album_id, fmt, has_lyrics, disc, tracktotal, raw_genres in rows:
        item_id = _coerce_int(raw_id)
        track_count += 1
        if (value := _coerce_optional_str(fmt)) is not None:
            formats.append(value)
        if not genre_fallback:
            genre_fallback = tuple(_genre_values(raw_genres))
        # A track is ANSWERED when it carries lyrics or beets flagged it
        # instrumental: an instrumental has no lyrics BY NATURE, so counting
        # it as missing left albums stuck at Partial with nothing to fetch.
        # ``has_lyrics`` is SQL's 0/1 answer to the first half — see
        # _ITEM_FACTS_SQL for why the text itself never comes back.
        if _coerce_int(has_lyrics) or item_id in instrumental:
            answered += 1
        total = _coerce_int(tracktotal)
        if track_count == 1:
            first_tracktotal = total
        if (disc_no := _coerce_int(disc)) not in seen_discs:
            seen_discs.add(disc_no)
            per_disc_tracktotal += total
    return _AlbumFacts(
        track_count=track_count,
        # A mixed-format album takes its most common value (one per album);
        # ``Counter`` breaks a tie on first appearance, i.e. play order.
        format=Counter(formats).most_common(1)[0][0] if formats else "Unknown",
        genre_fallback=genre_fallback,
        lyrics=_lyrics_bucket(answered, track_count),
        first_tracktotal=first_tracktotal,
        per_disc_tracktotal=per_disc_tracktotal,
    )


def _collect_facts(lib: Library) -> dict[int, _AlbumFacts]:
    """Per-album track facts for the whole library, in two queries.

    Replaces one ``album.items()`` query (plus a full beets ``Item`` build per
    track) per album — ~4.5k queries and 18k model instantiations on a real
    library, repaid after EVERY mutation because the cache is dropped on
    ``emit_library_changed``. Values come out of raw SQL, so they bypass beets'
    type layer entirely and every one goes through a ``_coerce_*`` helper.

    Track order is ``(disc, track, id)`` — play order, and deliberately NOT
    beets' ``sort_item`` display sort (``artist+ album+ disc+ track+`` by
    default), which the old ``album.items()`` build inherited. The two differ
    only WITHIN an album whose tracks carry different artists, and only for the
    order-sensitive facts below (genre fallback, a tied format vote, which
    disc's ``tracktotal`` comes first); a compilation's facets no longer shift
    with the user's display-sort preference.
    """
    with lib.transaction() as tx:
        item_rows = tx.query(_ITEM_FACTS_SQL, (_PYTHON_WHITESPACE,))
        instrumental_rows = tx.query(_FLEX_SQL, ("lyrics_instrumental",))

    # Value-tested, never presence-tested: beets writes the flag as FALSE (which
    # reads back as the TRUTHY string "0") on every track it DID find lyrics for.
    instrumental = {
        _coerce_int(entity_id)
        for entity_id, value in instrumental_rows
        if _instrumental_value(value)
    }

    facts: dict[int, _AlbumFacts] = {}
    for album_id, rows in groupby(item_rows, key=lambda row: _coerce_int(row["album_id"])):
        facts[album_id] = _album_facts(list(rows), instrumental)
    return facts


def _tracks_bucket(album: BeetsAlbum, facts: _AlbumFacts) -> str:
    """Complete / Incomplete / Unknown vs the matched release's track count.

    Mirrors beets' ``Album.albumtotal`` (the ``missing`` plugin's completeness
    source): expected = the first track's ``tracktotal`` for single-disc /
    non-per-disc numbering, else one ``tracktotal`` per distinct disc. As-is /
    unmatched imports carry no ``tracktotal`` -> ``Unknown``. Inherited beets
    caveat: an album missing an ENTIRE disc undercounts ``expected`` (no item
    exists to carry that disc's total).
    """
    if facts.track_count == 0:
        return "Unknown"
    disctotal = _coerce_int(album.get("disctotal"))
    # Read the flag by confuse truthiness, NOT .get(bool): a value beets tolerates
    # but that isn't a canonical bool (e.g. `per_disc_numbering: on`) makes the
    # bool template raise ConfigTypeError, crashing the whole browse-cache build.
    if disctotal <= 1 or not bool(config["per_disc_numbering"]):
        expected = facts.first_tracktotal
    else:
        expected = facts.per_disc_tracktotal
    if expected <= 0:
        return "Unknown"
    return "Complete" if facts.track_count >= expected else "Incomplete"


def _coerce_added(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]  # beets value is untyped
    except (TypeError, ValueError):
        return 0.0


def _build_row(album: BeetsAlbum, facts: _AlbumFacts) -> BrowseRow:
    albumartist = _coerce_str(album.albumartist)
    # ``_album_genre``'s heuristic, against facts instead of items: the album's
    # own genres win, else the FIRST track with any (not a majority vote).
    genres = _genre_values(album.get("genres")) or list(facts.genre_fallback)
    return BrowseRow(
        album_id=_require_id(album.id),
        artist_key=albumartist.casefold(),
        album_key=_coerce_str(album.album).casefold(),
        albumartist=albumartist,
        added=_coerce_added(album.get("added")),
        track_count=facts.track_count,
        # These two DELIBERATELY differ on a multi-genre album. ``genre_raw``
        # feeds the ``Album`` model, so it is beets' full display join
        # ("Gangsta Rap; Hip Hop; G-Funk"). The facet is the PRIMARY genre
        # alone: one representative value per album keeps the counts summing to
        # the album total, and buckets the user can actually click ("Rock", not
        # a seven-genre string only one album will ever match).
        genre_raw=_genre_join(genres),
        genre=genres[0] if genres else "Unknown",
        # "80s" means the music's era: original release year, falling back to
        # the (possibly reissue) release year when beets has no original_year.
        decade=_album_decade(_coerce_year(album.get("original_year")) or _coerce_year(album.year)),
        format=facts.format,
        album_type=_facet_str(album.get("albumtype")),
        source=_facet_str(album.get("data_source")),
        media=_facet_str(album.get("media")),
        country=_facet_str(album.get("country")),
        lyrics=facts.lyrics,
        tracks=_tracks_bucket(album, facts),
    )


def _rows(lib: Library) -> list[BrowseRow]:
    """Cached rows for this library, building with ONE full scan on a miss.

    The scan runs under ``_BUILD_LOCK`` only (so concurrent cold-cache calls
    don't duplicate it) and NEVER under ``_STATE_LOCK`` — invalidation must stay
    O(1) even mid-scan (see the lock comments above). The build snapshots the
    generation first and stores its rows only if no invalidation happened while
    it scanned; a mutated-under scan is served to its requester but not cached.
    """
    key = _cache_key(lib)
    with _STATE_LOCK:
        cached = _ROWS.get(key)
        if cached is not None:
            return cached
    with _BUILD_LOCK:
        # Another builder may have filled the cache while we waited for the
        # build lock — re-check before paying for a scan of our own.
        with _STATE_LOCK:
            cached = _ROWS.get(key)
            if cached is not None:
                return cached
            generation = _GENERATION
        facts = _collect_facts(lib)
        rows = [
            _build_row(album, facts.get(_require_id(album.id), _EMPTY_FACTS))
            for album in lib.albums()
        ]
        with _STATE_LOCK:
            if _GENERATION == generation:
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
