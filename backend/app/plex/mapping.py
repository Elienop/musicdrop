"""Resolve playlist tracks to Plex Track objects.

Plex exposes no server-side file-path filter, so we scan the music section's
tracks once and build three indexes from that single pass:

- by file path (``Track.locations``) -> the exact, co-located match,
- by ``(album-artist, title)`` -> the metadata fallback for when MusicDrop's
  beets library and Plex hold separately-organized copies of the same music, and
- by ``(album, title)`` -> the last resort for when Plex's own agent REWROTE the
  artist name, sometimes into a different script (beets ``Wael Kfoury`` vs Plex
  ``وائل كفوري``), which no casefold can bridge.

A track is resolved in that order, and the order is contract: whatever resolves
by path today keeps resolving by path. Ambiguity always refuses -- a track that
cannot be singled out is left missing, never guessed.

The third index carries a hazard the first two do not. Singles have album ==
title, so ``(album, title)`` degenerates into a TITLE-ONLY key for exactly the
tracks that motivated it -- and two artists with a single called "Habibi"
collide there. So no candidate is ever accepted on album and title alone: the
lengths have to agree too, and a length either side does not know refuses
outright. See ``_album_match``.

Every miss is reported with its identity and WHY it missed, so the UI can point
at the row.
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
    to fall back when the path isn't found.

    ``length_seconds`` is the track's length in SECONDS (beets' unit), or
    ``None`` when it is unknown -- Plex reports its own duration in
    MILLISECONDS, so the two are converted where they meet, never here."""

    item_id: int
    path: str
    albumartist: str
    album: str
    title: str
    track: int | None
    length_seconds: float | None


@dataclass(frozen=True)
class PlexResolution:
    """``tracks`` are the resolved Plex Track objects in playlist order (misses
    dropped); ``missing`` is every spec that resolved to nothing, in order."""

    tracks: list[Any]
    missing: list[PlexMissingTrack]


# How far two lengths for the SAME recording may sit apart, in seconds. The same
# track in two files disagrees by well under a second (lossy encoders add
# padding; Plex rounds a container duration where beets reads the stream), and a
# remaster or a different rip of one CD track drifts by about a second more. Two
# seconds covers that with room to spare while staying far below the gap that
# separates two DIFFERENT recordings sharing an album and a title -- a radio
# edit, a live take, a remix. Widening it is what re-opens the guessing hole
# this fallback exists to close, so it is deliberately not configurable.
_LENGTH_TOLERANCE_SECONDS = 2.0


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


def _seconds(value: object) -> float | None:
    """A length in seconds, or ``None`` when there isn't one.

    Zero reads as ABSENT, not as "zero seconds": beets stores 0.0 for a length it
    never read and Plex reports 0 for a file it has not analysed. Taken
    literally, those two zeros agree perfectly — and the whole point of comparing
    lengths is to refuse a candidate whose length we cannot vouch for.
    """
    if not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _plex_seconds(track: Any) -> float | None:
    """``Track.duration`` in SECONDS. Plex reports MILLISECONDS (plexapi casts
    the ``duration`` attrib to int); beets' ``length`` is seconds. The two units
    meet here and nowhere else."""
    millis = _seconds(getattr(track, "duration", None))
    return None if millis is None else millis / 1000.0


@dataclass(frozen=True)
class _Indexes:
    """What one library scan builds: the three keys a spec is tried against, in
    the order ``resolve_ordered_tracks`` tries them."""

    by_path: dict[str, Any]
    by_artist_title: dict[tuple[str, str], list[Any]]
    by_album_title: dict[tuple[str, str], list[Any]]


def _build_indexes(section: Any) -> _Indexes:
    """ONE scan of the section -> all three lookups."""
    by_path: dict[str, Any] = {}
    by_artist_title: dict[tuple[str, str], list[Any]] = {}
    by_album_title: dict[tuple[str, str], list[Any]] = {}
    for track in section.searchTracks():
        for location in track.locations:
            by_path[location] = track
        title = _norm(_attr(track, "title"))
        by_artist_title.setdefault((_norm(_attr(track, "grandparentTitle")), title), []).append(
            track
        )
        by_album_title.setdefault((_norm(_attr(track, "parentTitle")), title), []).append(track)
    return _Indexes(by_path=by_path, by_artist_title=by_artist_title, by_album_title=by_album_title)


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


def _album_match(
    by_album_title: dict[tuple[str, str], list[Any]], spec: PlexTrackSpec
) -> tuple[Any | None, MissReason | None]:
    """Resolve an album-artist miss by ``(album, title)`` AGREED ON LENGTH.

    The case this exists for: Plex's agent rewrote the artist into another
    script, so beets' ``Wael Kfoury`` and Plex's ``وائل كفوري`` can never share
    an ``(album-artist, title)`` key — while the album title came through
    untouched.

    The trap, and why every accepted candidate has to agree on length: many of
    those releases are SINGLES, where album == title, so this key collapses into
    a title-only one for exactly the tracks it was built for. Two artists with a
    single called "Habibi" then collide, and if only one of them is in Plex a
    key-only match hands back the WRONG RECORDING and reports the sync "ok". A
    length that agrees is what turns the key from a guess into evidence; a
    length either side does not know is no evidence at all, so it refuses.

    Returns ``(track, None)`` on the one surviving candidate;
    ``(None, "ambiguous")`` when several survive — the caller leaves the row
    missing rather than picking one; ``(None, "not_found")`` when the key is
    incomplete, the length is unknown, or nothing survives.
    """
    key = (_norm(spec.album), _norm(spec.title))
    length = _seconds(spec.length_seconds)
    if not all(key) or length is None:
        return None, "not_found"
    pool: list[Any] = []
    for cand in by_album_title.get(key, ()):
        cand_length = _plex_seconds(cand)
        if cand_length is not None and abs(cand_length - length) <= _LENGTH_TOLERANCE_SECONDS:
            pool.append(cand)
    if len(pool) == 1:
        return pool[0], None
    return (None, "ambiguous") if pool else (None, "not_found")


def resolve_ordered_tracks(section: Any, specs: list[PlexTrackSpec]) -> PlexResolution:
    """Resolve ordered specs to Track objects (one library scan), reporting
    every miss with its identity and reason."""
    indexes = _build_indexes(section)
    tracks: list[Any] = []
    missing: list[PlexMissingTrack] = []
    for spec in specs:
        track = indexes.by_path.get(spec.path)
        reason: MissReason | None = None
        if track is None:
            track, reason = _meta_match(indexes.by_artist_title, spec)
        if track is None and reason != "ambiguous":
            # Only a CLEAN miss drops to the album key. "ambiguous" means the
            # album-artist index found this track and could not tell its copies
            # apart — and the album key is WIDER than the one that just refused
            # (it drops the artist constraint), so letting a length pick one of
            # those tied copies would be guessing with a weaker key after a
            # stronger one declined. It would also downgrade an honest "found it
            # twice" into "not found" whenever the lengths were unknown.
            track, reason = _album_match(indexes.by_album_title, spec)
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
