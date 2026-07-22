"""Library stats for the home dashboard (GET /api/stats).

All beets access for the feature lives here, reusing helpers from the sibling
``app/beets/library.py`` (CLAUDE.md rule 3). Pure reads — no mutation, no jobs.
``total_bytes`` is an ESTIMATE (``bitrate * length / 8``, beets' own non-exact
``stats`` method) so the endpoint stays a couple of cheap DB passes with no
filesystem walk.
"""

from __future__ import annotations

import heapq
from typing import Any

from app.beets.library import _to_album
from app.models.album import Album
from app.models.stats import LibraryStats, LibraryStatsResponse


def compute_stats(lib: Any) -> LibraryStats:
    """Counts + total duration + estimated total size.

    Item-level sums come from ONE SQL aggregate (no per-track beets Model build —
    the whole item table used to be materialized on every Home load). Album/artist
    counts come from the shared ``BrowseRow`` cache, so ``artist_count`` matches
    :func:`list_artists` exactly (same rows, same blank-albumartist skip).
    ``CAST(... AS INTEGER)`` truncates per row, mirroring the old
    ``int(bitrate * length / 8)`` per-track math; NULL length/bitrate rows drop out
    of ``SUM`` exactly as the old code contributed 0 for them.
    """
    from app.beets.browse import _rows

    with lib.transaction() as tx:
        row = tx.query(
            "SELECT COUNT(*), COALESCE(SUM(length), 0), "
            "COALESCE(SUM(CAST(bitrate * length / 8 AS INTEGER)), 0) FROM items"
        )[0]

    rows = _rows(lib)
    artists = {r.albumartist for r in rows if r.albumartist.strip()}

    return LibraryStats(
        track_count=int(row[0]),
        album_count=len(rows),
        artist_count=len(artists),
        total_seconds=float(row[1]),
        total_bytes=int(row[2]),
    )


def recent_albums(lib: Any, *, limit: int = 8) -> list[Album]:
    """The newest albums by ``added`` (desc), mapped to the wire ``Album``.

    Picks the top-``limit`` album ids from the shared ``BrowseRow`` cache (by
    ``added``) via ``heapq.nlargest`` and loads ONLY those from beets — the old
    path materialized every album just to keep 8.
    """
    from app.beets.browse import _rows

    winners = heapq.nlargest(limit, _rows(lib), key=lambda r: r.added)
    albums: list[Album] = []
    for r in winners:
        album = lib.get_album(r.album_id)
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
