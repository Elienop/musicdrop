"""Map track file paths (as Plex sees them) to Plex ratingKeys.

Plex exposes no server-side file-path filter, so we scan the music section's
tracks once and index every ``Track.locations`` entry to its ``ratingKey``.
Callers resolving many paths should build the index once and reuse it.
"""

from __future__ import annotations

from typing import Any


def index_tracks_by_path(section: Any) -> dict[str, int]:
    index: dict[str, int] = {}
    for track in section.searchTracks():
        rating_key = int(track.ratingKey)
        for location in track.locations:
            index[location] = rating_key
    return index


def resolve_ordered_tracks(section: Any, plex_paths: list[str]) -> tuple[list[Any], int]:
    """Resolve ordered Plex file paths to Track objects (one library scan).

    Returns ``(tracks_in_order, missing_count)`` — ``missing_count`` is the
    number of paths with no matching Track in Plex (dropped from the result)."""
    index: dict[str, Any] = {}
    for track in section.searchTracks():
        for location in track.locations:
            index[location] = track
    tracks: list[Any] = []
    missing = 0
    for path in plex_paths:
        track = index.get(path)
        if track is None:
            missing += 1
        else:
            tracks.append(track)
    return tracks, missing
