"""Pydantic models for the library dashboard (GET /api/stats)."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.album import Album


class LibraryStats(BaseModel):
    """Headline library counts. ``total_bytes`` is an ESTIMATE
    (``sum(bitrate * length / 8)``), surfaced with ``size_is_estimate`` on the
    response so the UI can render it with a leading ``~``."""

    track_count: int
    album_count: int
    artist_count: int
    total_seconds: float
    total_bytes: int


class LibraryStatsResponse(BaseModel):
    """Response of ``GET /api/stats`` — headline stats plus the newest albums
    (reusing the existing :class:`Album` shape the roster already renders)."""

    stats: LibraryStats
    recently_added: list[Album]
    size_is_estimate: bool = True
