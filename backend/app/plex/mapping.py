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
