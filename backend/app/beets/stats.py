"""Library stats for the home dashboard (GET /api/stats).

All beets access for the feature lives here, reusing helpers from the sibling
``app/beets/library.py`` (CLAUDE.md rule 3). Pure reads — no mutation, no jobs.
``total_bytes`` is an ESTIMATE (``bitrate * length / 8``, beets' own non-exact
``stats`` method) so the endpoint stays a couple of cheap DB passes with no
filesystem walk.

Deliberately independent of the shared ``BrowseRow`` cache
(``app.beets.browse._rows``): that cache is dropped on every library mutation
and its rebuild is a multi-second full scan, so a stats path riding it would
make the FIRST Overview load after any change pay that cost. Instead this
module answers straight from SQL aggregates plus, for the "recently added"
rail, at most ``limit`` targeted ``lib.get_album`` loads.
"""

from __future__ import annotations

from typing import Any

from app.beets.library import _to_album
from app.models.album import Album
from app.models.stats import LibraryStats, LibraryStatsResponse


def compute_stats(lib: Any) -> LibraryStats:
    """Counts + total duration + estimated total size.

    Item-level sums and the album count come from SQL aggregates (no per-track
    or per-album beets Model build). ``artist_count`` counts DISTINCT exact
    ``albumartist`` strings, skipping blanks via a Python-side ``.strip()`` (
    SQLite's ``TRIM`` only strips spaces, not all whitespace) — matching
    :func:`app.beets.library.list_artists`'s grouping exactly (same distinct
    strings, same blank skip; that function's diacritic-insensitive SORT does
    not change which rows count). ``CAST(... AS INTEGER)`` truncates per row,
    mirroring the old ``int(bitrate * length / 8)`` per-track math; NULL
    length/bitrate rows drop out of ``SUM`` exactly as the old code
    contributed 0 for them.
    """
    with lib.transaction() as tx:
        row = tx.query(
            "SELECT COUNT(*), COALESCE(SUM(length), 0), "
            "COALESCE(SUM(CAST(bitrate * length / 8 AS INTEGER)), 0) FROM items"
        )[0]
        album_count = int(tx.query("SELECT COUNT(*) FROM albums")[0][0])
        artist_rows = tx.query("SELECT DISTINCT albumartist FROM albums")

    artist_count = len({v for (v,) in artist_rows if isinstance(v, str) and v.strip()})

    return LibraryStats(
        track_count=int(row[0]),
        album_count=album_count,
        artist_count=artist_count,
        total_seconds=float(row[1]),
        total_bytes=int(row[2]),
    )


def recent_albums(lib: Any, *, limit: int = 8) -> list[Album]:
    """The newest albums by ``added`` (desc), mapped to the wire ``Album``.

    Picks the top-``limit`` album ids straight from SQL (``added DESC, id
    DESC`` — the ``id`` tiebreak makes the order deterministic for albums
    added in the same instant) and loads ONLY those from beets, so at most
    ``limit`` albums are ever materialized regardless of library size.
    """
    with lib.transaction() as tx:
        id_rows = tx.query("SELECT id FROM albums ORDER BY added DESC, id DESC LIMIT ?", (limit,))

    albums: list[Album] = []
    for (album_id,) in id_rows:
        album = lib.get_album(int(album_id))
        if album is not None:
            albums.append(_to_album(album))
    return albums


def build_stats_response(lib: Any, *, recent_limit: int = 8) -> LibraryStatsResponse:
    """Assemble the full ``GET /api/stats`` payload. ``compute_stats`` and
    ``recent_albums`` each run their own cheap query (kept independent for
    testability; both are DB reads, no perf concern at one-user scale)."""
    return LibraryStatsResponse(
        stats=compute_stats(lib),
        recently_added=recent_albums(lib, limit=recent_limit),
    )
