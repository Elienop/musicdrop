"""Map beets autotag match objects to our import Pydantic models.

Pure functions, no I/O and no threads. Imports beets — allowed because this
module lives inside the beets-adapter boundary (CLAUDE.md rule 3). Everything
returned is one of app.models.import_models, so beets' AlbumMatch/Distance/
AlbumInfo/TrackInfo never leak past here.
"""

from __future__ import annotations

import os
from typing import Any

from beets.autotag import AlbumMatch
from beets.autotag.match import Recommendation as BeetsRec
from beets.util import get_most_common_tags
from mediafile import MediaFile

from app.models.import_models import (
    AlbumChange,
    Candidate,
    CandidateOption,
    MissingTrack,
    Recommendation,
    TrackChange,
    TrackChangeStatus,
    UnmatchedItem,
)


def _confidence(distance: Any) -> float:
    """beets distance (0.0 = perfect) -> a confidence percentage.

    Mirrors beets' own display: ``(1 - distance) * 100`` (autotag/distance.py).
    """
    return round((1.0 - float(distance)) * 100.0, 1)


# beets IntEnum -> our string enum (only the album-level levels are needed).
# Lives here (not import_session) so session-less lookup code can map
# recommendations without importing the session module.
_REC_MAP = {
    BeetsRec.none: Recommendation.none,
    BeetsRec.low: Recommendation.low,
    BeetsRec.medium: Recommendation.medium,
    BeetsRec.strong: Recommendation.strong,
}


def coverartarchive_front_url(*, data_source: str | None, album_id: str | None) -> str | None:
    """The Cover Art Archive front-image URL for a MusicBrainz release, or None.

    CAA is keyed by the MusicBrainz release MBID (``AlbumInfo.album_id``). Only
    MusicBrainz matches have one; other sources (Discogs, etc.) return None and
    the UI shows a placeholder. ``front-500`` is CAA's 500px thumbnail rendition.
    """
    if data_source is None or album_id is None:
        return None
    if data_source.strip().lower() != "musicbrainz":
        return None
    mbid = str(album_id).strip()
    if not mbid:
        return None
    return f"https://coverartarchive.org/release/{mbid}/front-500"


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_disambig(value: Any) -> str | None:
    """beets' ``Match.disambig_string`` str()-joins its disambiguation fields,
    so missing values become literal ``"None"`` segments. Drop them (and
    blanks); an all-dropped string collapses to ``None``."""
    text = _opt_str(value)
    if text is None:
        return None
    parts = [p for p in (s.strip() for s in text.split(",")) if p and p != "None"]
    return ", ".join(parts) or None


def embedded_art(path: str) -> tuple[bytes, str] | None:
    """The first embedded cover image ``(bytes, mime)`` for a media file, or None.

    Used to render the "before" (current files) cover during review. Returns None
    for a missing file, an unreadable file, or one with no embedded image. A
    declared-but-empty mime falls back to ``application/octet-stream`` so the FE
    ``<img>`` cleanly degrades to a placeholder rather than rendering garbage.
    """
    if not os.path.isfile(path):
        return None
    try:
        images = MediaFile(path).images
    except Exception:  # any mediafile read error => no art
        return None
    if not images:
        return None
    image = images[0]
    mime = _opt_str(image.mime_type) or "application/octet-stream"
    return bytes(image.data), mime


def _opt_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _album_change_from_info(info: Any) -> AlbumChange:
    """The proposed (after) album-level identity fields from an AlbumInfo."""
    return AlbumChange(
        artist=_opt_str(info.artist),
        album=_opt_str(info.album),
        year=_opt_int(info.year),
        label=_opt_str(info.label),
        country=_opt_str(info.country),
        media=_opt_str(info.media),
    )


def _album_change_from_current(
    items: list[Any], cur_artist: str | None, cur_album: str | None
) -> AlbumChange:
    """The current (before) album-level fields from the user's files.

    Uses beets' own consensus of the items (``get_most_common_tags``) for the
    fields that have no single task-level attribute, and the task's
    ``cur_artist``/``cur_album`` for identity (already computed by beets).
    """
    likelies, _ = get_most_common_tags(items)
    return AlbumChange(
        artist=_opt_str(cur_artist),
        album=_opt_str(cur_album),
        year=_opt_int(likelies.get("year")),
        label=_opt_str(likelies.get("label")),
        country=_opt_str(likelies.get("country")),
        media=_opt_str(likelies.get("media")),
    )


def _track_changes(match: AlbumMatch) -> list[TrackChange]:
    """Per-track current->proposed rows from AlbumMatch.mapping.

    Ordered by the proposed track index so the tracklist reads in release order.
    A row is ``changed`` when the matched TrackInfo has a non-zero per-field
    title distance or the track number differs; otherwise ``unchanged``.
    """
    rows: list[TrackChange] = []
    for item, track_info in match.mapping.items():
        track_dist = match.distance.tracks.get(track_info)
        # Distance.keys() omits zero-valued penalties, so a present "track_title"
        # already means the title differs (no need to re-compare against 0).
        title_changed = track_dist is not None and "track_title" in track_dist.keys()
        track_before = _opt_int(item.track)
        track_after = _opt_int(track_info.index)
        number_changed = track_before != track_after
        status = (
            TrackChangeStatus.changed
            if (title_changed or number_changed)
            else TrackChangeStatus.unchanged
        )
        rows.append(
            TrackChange(
                index=track_after,
                status=status,
                title_before=_opt_str(item.title),
                title_after=_opt_str(track_info.title),
                track_before=track_before,
                track_after=track_after,
                format=_opt_str(item.format),
            )
        )
    rows.sort(key=lambda r: (r.index is None, r.index or 0))
    return rows


def _missing_tracks(match: AlbumMatch) -> list[MissingTrack]:
    """Release tracks with no local file (AlbumMatch.extra_tracks)."""
    return [
        MissingTrack(index=_opt_int(t.index), title=_opt_str(t.title)) for t in match.extra_tracks
    ]


def _unmatched_items(match: AlbumMatch) -> list[UnmatchedItem]:
    """Local files with no release track (AlbumMatch.extra_items)."""
    return [
        UnmatchedItem(title=_opt_str(i.title), track=_opt_int(i.track), format=_opt_str(i.format))
        for i in match.extra_items
    ]


def map_candidate_options(
    candidates: list[AlbumMatch],
) -> list[CandidateOption]:
    """Map the ranked ``task.candidates`` to the switcher options — all of them.

    Each option carries its OWN full per-release diff (album_after,
    changed_fields, tracks, missing, unmatched, cover_after_url, data_url) built
    from the SAME per-release block builders the top-match ``Candidate`` uses, so
    the review screen can re-render the whole preview for the selected release.
    ``options[0]`` is the canonical top and its diff equals the ``Candidate``'s
    top diff by construction. No MusicDrop-side cap: beets owns the candidate
    count (its per-source ``search_limit``), and ``choose_match`` applies over
    this SAME full list, so the switcher and the apply-able set stay in lock-step.
    """
    options: list[CandidateOption] = []
    for index, match in enumerate(candidates):
        data_source = _opt_str(match.info.data_source)
        album_id = _opt_str(getattr(match.info, "album_id", None))
        options.append(
            CandidateOption(
                index=index,
                confidence=_confidence(match.distance),
                data_source=data_source,
                disambiguation=_clean_disambig(match.disambig_string),
                release_id=album_id,
                # Per-option identity, derived EXACTLY as album_after is
                # (_album_change_from_info), so the bank's duplicate check on a
                # non-top selection matches the apply on that same release.
                album_artist=_opt_str(match.info.artist),
                album=_opt_str(match.info.album),
                year=_opt_int(match.info.year),
                # This option's full before/after diff — the same builders the
                # top-match Candidate uses, applied to THIS release.
                album_after=_album_change_from_info(match.info),
                changed_fields=list(match.distance.generic_penalty_keys),
                tracks=_track_changes(match),
                missing=_missing_tracks(match),
                unmatched=_unmatched_items(match),
                cover_after_url=coverartarchive_front_url(
                    data_source=data_source, album_id=album_id
                ),
                data_url=_opt_str(match.info.data_url),
            )
        )
    return options


def map_album_match(
    match: AlbumMatch,
    *,
    cur_artist: str | None,
    cur_album: str | None,
    options: list[CandidateOption],
    recommendation: Recommendation = Recommendation.none,
    has_current_art: bool = False,
) -> Candidate:
    """Map a beets AlbumMatch (+ current task state) to a Candidate.

    ``options`` is the already-mapped ranked alternative list (pass [] when not
    needed); ``recommendation`` mirrors ``task.rec`` for the album.
    """
    return Candidate(
        confidence=_confidence(match.distance),
        recommendation=recommendation,
        data_source=_opt_str(match.info.data_source),
        data_url=_opt_str(match.info.data_url),
        cover_after_url=coverartarchive_front_url(
            data_source=_opt_str(match.info.data_source),
            album_id=_opt_str(getattr(match.info, "album_id", None)),
        ),
        has_current_art=has_current_art,
        changed_fields=list(match.distance.generic_penalty_keys),
        album_before=_album_change_from_current(
            list(match.mapping.keys()) + list(match.extra_items),
            cur_artist,
            cur_album,
        ),
        album_after=_album_change_from_info(match.info),
        tracks=_track_changes(match),
        missing=_missing_tracks(match),
        unmatched=_unmatched_items(match),
        options=options,
    )
