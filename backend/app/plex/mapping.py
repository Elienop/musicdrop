"""Resolve playlist tracks to Plex Track objects.

Plex exposes no server-side file-path filter, so we scan the music section's
tracks once and build two indexes from that single pass:

- by file path (``Track.locations``) -> the exact, co-located match, and
- by ``(album-artist, title)`` -> the metadata fallback for when MusicDrop's
  beets library and Plex hold separately-organized copies of the same music.

A track is resolved path-first, then by metadata (album then track-number
tiebreak; ambiguous -> left missing, never guessed). Every miss is reported
with its identity and WHY it missed, so the UI can point at the row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.models.plex import PlexMissingTrack

MissReason = Literal["not_found", "ambiguous"]


@dataclass(frozen=True)
class PlexTrackSpec:
    """One ordered playlist track: the beets item id (so a miss can be pointed
    at), its path as Plex sees it (already translated), plus the metadata used
    to fall back when the path isn't found."""

    item_id: int
    path: str
    albumartist: str
    album: str
    title: str
    track: int | None


@dataclass(frozen=True)
class PlexResolution:
    """``tracks`` are the resolved Plex Track objects in playlist order (misses
    dropped); ``missing`` is every spec that resolved to nothing, in order."""

    tracks: list[Any]
    missing: list[PlexMissingTrack]


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
) -> tuple[Any | None, MissReason | None]:
    """Resolve a path-missed spec by metadata.

    Returns ``(track, None)`` on a match; ``(None, "not_found")`` when the key is
    incomplete (a title-only match is a guess) or no candidate exists; and
    ``(None, "ambiguous")`` when candidates exist but album, then track number,
    cannot single one out."""
    key = (_norm(spec.albumartist), _norm(spec.title))
    if not all(key):
        return None, "not_found"
    cands = by_meta.get(key)
    if not cands:
        return None, "not_found"
    if len(cands) == 1:
        return cands[0], None
    album_pool = [c for c in cands if _norm(_attr(c, "parentTitle")) == _norm(spec.album)]
    pool = album_pool or cands
    if len(pool) == 1:
        return pool[0], None
    if spec.track is not None:
        track_pool = [c for c in pool if _track_no(c) == spec.track]
        if len(track_pool) == 1:
            return track_pool[0], None
    return None, "ambiguous"


def resolve_ordered_tracks(section: Any, specs: list[PlexTrackSpec]) -> PlexResolution:
    """Resolve ordered specs to Track objects (one library scan), reporting
    every miss with its identity and reason."""
    by_path, by_meta = _build_indexes(section)
    tracks: list[Any] = []
    missing: list[PlexMissingTrack] = []
    for spec in specs:
        track = by_path.get(spec.path)
        reason: MissReason | None = None
        if track is None:
            track, reason = _meta_match(by_meta, spec)
        if track is None:
            missing.append(
                PlexMissingTrack(
                    item_id=spec.item_id,
                    title=spec.title,
                    albumartist=spec.albumartist,
                    album=spec.album,
                    reason=reason or "not_found",
                )
            )
        else:
            tracks.append(track)
    return PlexResolution(tracks=tracks, missing=missing)
