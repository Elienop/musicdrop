"""Resolve playlist tracks to Plex Track objects.

Plex exposes no server-side file-path filter, so we scan the music section's
tracks once and build two indexes from that single pass:

- by file path (``Track.locations``) -> the exact, co-located match, and
- by ``(album-artist, title)`` -> the metadata fallback for when MusicDrop's
  beets library and Plex hold separately-organized copies of the same music.

A track is resolved path-first, then by metadata (album then track-number
tiebreak; ambiguous -> left missing, never guessed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlexTrackSpec:
    """One ordered playlist track: its path as Plex sees it (already
    translated) plus the metadata used to fall back when the path isn't found."""

    path: str
    albumartist: str
    album: str
    title: str
    track: int | None


def _norm(value: str) -> str:
    """casefold + collapse whitespace + strip — the metadata comparison key."""
    return " ".join(value.casefold().split())


def _attr(track: Any, name: str) -> str:
    value = getattr(track, name, "")
    return value if isinstance(value, str) else ""


def _track_no(track: Any) -> int | None:
    index = getattr(track, "index", None)
    if isinstance(index, int):
        return index
    if isinstance(index, str) and index.isdigit():
        return int(index)
    return None


def index_tracks_by_path(section: Any) -> dict[str, int]:
    index: dict[str, int] = {}
    for track in section.searchTracks():
        rating_key = int(track.ratingKey)
        for location in track.locations:
            index[location] = rating_key
    return index


def _build_indexes(
    section: Any,
) -> tuple[dict[str, Any], dict[tuple[str, str], list[Any]]]:
    """One scan -> (path -> Track, (album-artist, title) -> [Track])."""
    by_path: dict[str, Any] = {}
    by_meta: dict[tuple[str, str], list[Any]] = {}
    for track in section.searchTracks():
        for location in track.locations:
            by_path[location] = track
        key = (_norm(_attr(track, "grandparentTitle")), _norm(_attr(track, "title")))
        by_meta.setdefault(key, []).append(track)
    return by_path, by_meta


def _meta_match(
    by_meta: dict[tuple[str, str], list[Any]], spec: PlexTrackSpec
) -> Any | None:
    """Resolve a path-missed spec by metadata, or None when ambiguous/absent.

    Unique album-artist+title -> that track. Otherwise narrow by album, then by
    track number; a single survivor wins, anything still tied is left missing
    (never guessed)."""
    key = (_norm(spec.albumartist), _norm(spec.title))
    if not any(key):  # no metadata to match on -> only an exact path can resolve it
        return None
    cands = by_meta.get(key)
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    album_pool = [c for c in cands if _norm(_attr(c, "parentTitle")) == _norm(spec.album)]
    pool = album_pool or cands
    if len(pool) == 1:
        return pool[0]
    if spec.track is not None:
        track_pool = [c for c in pool if _track_no(c) == spec.track]
        if len(track_pool) == 1:
            return track_pool[0]
    return None


def resolve_ordered_tracks(
    section: Any, specs: list[PlexTrackSpec]
) -> tuple[list[Any], int]:
    """Resolve ordered specs to Track objects (one library scan).

    Each spec is matched by exact path first, then by metadata. Returns
    ``(tracks_in_order, missing_count)`` — ``missing_count`` is the number of
    specs that matched neither (dropped from the result)."""
    by_path, by_meta = _build_indexes(section)
    tracks: list[Any] = []
    missing = 0
    for spec in specs:
        track = by_path.get(spec.path)
        if track is None:
            track = _meta_match(by_meta, spec)
        if track is None:
            missing += 1
        else:
            tracks.append(track)
    return tracks, missing
