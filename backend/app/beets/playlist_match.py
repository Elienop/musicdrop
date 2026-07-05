"""Match import source entries against the beets library.

One ``lib.items()`` scan builds the index; entries then match in tiers:
T1 unique normalized filename -> T2 unique normalized artist+title (a ±3s
duration check may break a tie) -> otherwise ambiguous/unmatched with up to
3 deterministic suggestions. Never auto-accepts a non-unique match. Reuses
``normalize`` from the duplicates feature so "Song (Remastered 2011)"
matches "Song". beets imports are allowed here (CLAUDE.md rule 3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from beets.library import Library

from app.beets.duplicates import normalize
from app.beets.library import _coerce_duration, _coerce_str
from app.models.playlist_import import ImportEntryPreview, SourceEntry, TrackSummary

_DURATION_TOLERANCE_S = 3.0
_MAX_SUGGESTIONS = 3

_Status = Literal["matched", "ambiguous", "unmatched"]


@dataclass
class _IndexedTrack:
    item_id: int
    title: str
    artist: str
    album: str
    duration_seconds: float | None


@dataclass
class MatchIndex:
    by_filename: dict[str, list[_IndexedTrack]] = field(default_factory=dict)
    by_artist_title: dict[tuple[str, str], list[_IndexedTrack]] = field(default_factory=dict)
    by_title: dict[str, list[_IndexedTrack]] = field(default_factory=dict)


def _stem(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def build_match_index(lib: Library) -> MatchIndex:
    """ONE library scan -> the three lookup maps."""
    index = MatchIndex()
    for item in lib.items():
        track = _IndexedTrack(
            item_id=int(item.id),
            title=_coerce_str(item.title),
            artist=_coerce_str(item.artist),
            album=_coerce_str(item.album),
            duration_seconds=_coerce_duration(item.length),
        )
        if item.path:
            key = normalize(_stem(os.fsdecode(item.path)))
            if key:
                index.by_filename.setdefault(key, []).append(track)
        title_key = normalize(track.title)
        if title_key:
            index.by_title.setdefault(title_key, []).append(track)
            artist_key = normalize(track.artist)
            if artist_key:
                index.by_artist_title.setdefault((artist_key, title_key), []).append(track)
    return index


def _summary(track: _IndexedTrack) -> TrackSummary:
    return TrackSummary(
        item_id=track.item_id,
        title=track.title,
        artist=track.artist,
        album=track.album,
        duration_seconds=track.duration_seconds,
    )


def _ordered(tracks: list[_IndexedTrack]) -> list[_IndexedTrack]:
    return sorted(tracks, key=lambda t: (t.artist.casefold(), t.album.casefold(), t.item_id))


def _within_tolerance(track: _IndexedTrack, duration: float | None) -> bool:
    return (
        duration is not None
        and track.duration_seconds is not None
        and abs(track.duration_seconds - duration) <= _DURATION_TOLERANCE_S
    )


def _preview(
    entry: SourceEntry,
    status: _Status,
    *,
    match: _IndexedTrack | None = None,
    suggestions: list[_IndexedTrack] | None = None,
) -> ImportEntryPreview:
    return ImportEntryPreview(
        position=entry.position,
        source=entry.source,
        artist=entry.artist,
        title=entry.title,
        album=entry.album,
        duration_seconds=entry.duration_seconds,
        status=status,
        item_id=match.item_id if match else None,
        match=_summary(match) if match else None,
        suggestions=[_summary(t) for t in _ordered(suggestions or [])[:_MAX_SUGGESTIONS]],
    )


def match_entries(index: MatchIndex, entries: list[SourceEntry]) -> list[ImportEntryPreview]:
    results: list[ImportEntryPreview] = []
    for entry in entries:
        results.append(_match_one(index, entry))
    return results


def _match_one(index: MatchIndex, entry: SourceEntry) -> ImportEntryPreview:
    leftovers: list[_IndexedTrack] = []
    # T1 - unique normalized filename.
    if entry.path:
        key = normalize(_stem(entry.path))
        hits = index.by_filename.get(key, []) if key else []
        if len(hits) == 1:
            return _preview(entry, "matched", match=hits[0])
        leftovers.extend(hits)  # non-unique T1 candidates become suggestions
    # T2 - unique normalized artist+title (duration may break a tie).
    if entry.artist and entry.title:
        key2 = (normalize(entry.artist), normalize(entry.title))
        hits = index.by_artist_title.get(key2, [])
        if len(hits) == 1:
            return _preview(entry, "matched", match=hits[0])
        if len(hits) > 1:
            close = [t for t in hits if _within_tolerance(t, entry.duration_seconds)]
            if len(close) == 1:
                return _preview(entry, "matched", match=close[0])
            return _preview(entry, "ambiguous", suggestions=hits + leftovers)
    # T3 - suggestions only.
    if entry.title:
        leftovers.extend(index.by_title.get(normalize(entry.title), []))
    deduped: dict[int, _IndexedTrack] = {t.item_id: t for t in leftovers}
    return _preview(entry, "unmatched", suggestions=list(deduped.values()))
