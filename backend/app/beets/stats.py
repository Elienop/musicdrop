"""Library stats for the home dashboard (GET /api/stats).

All beets access for the feature lives here, reusing helpers from the sibling
``app/beets/library.py`` (CLAUDE.md rule 3). Pure reads — no mutation, no jobs.
``total_bytes`` is an ESTIMATE (``bitrate * length / 8``, beets' own non-exact
``stats`` method) so the endpoint stays a couple of cheap DB passes with no
filesystem walk.
"""

from __future__ import annotations

from typing import Any

from app.beets.library import _coerce_str, _to_album
from app.models.album import Album
from app.models.stats import LibraryStats, LibraryStatsResponse


def compute_stats(lib: Any) -> LibraryStats:
    """Counts + total duration + estimated total size, in two DB passes."""
    track_count = 0
    total_seconds = 0.0
    total_bytes = 0
    for item in lib.items():
        length = float(item.length or 0.0)
        bitrate = int(item.bitrate or 0)
        track_count += 1
        total_seconds += length
        total_bytes += int(bitrate * length / 8)

    album_count = 0
    # Match ``list_artists`` exactly (it skips blank/whitespace album artists),
    # so ``artist_count`` equals the number of rows the roster shows below.
    artists: set[str] = set()
    for album in lib.albums():
        album_count += 1
        name = _coerce_str(album.albumartist)
        if not name.strip():
            continue
        artists.add(name)

    return LibraryStats(
        track_count=track_count,
        album_count=album_count,
        artist_count=len(artists),
        total_seconds=total_seconds,
        total_bytes=total_bytes,
    )


def recent_albums(lib: Any, *, limit: int = 8) -> list[Album]:
    """The newest albums by ``added`` (desc), mapped to the wire ``Album``."""
    albums = list(lib.albums())
    albums.sort(key=lambda a: a.added or 0.0, reverse=True)
    return [_to_album(a) for a in albums[:limit]]


def build_stats_response(lib: Any, *, recent_limit: int = 8) -> LibraryStatsResponse:
    """Assemble the full ``GET /api/stats`` payload. ``compute_stats`` and
    ``recent_albums`` each run their own cheap query (kept independent for
    testability; both are DB reads, no perf concern at one-user scale)."""
    return LibraryStatsResponse(
        stats=compute_stats(lib),
        recently_added=recent_albums(lib, limit=recent_limit),
    )
