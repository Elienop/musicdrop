"""Pydantic contract for the beets import flow.

These models are the stable surface the API/UI will consume. They are mapped
from beets' AlbumMatch/Distance/AlbumInfo/TrackInfo in app/beets/import_mapping.py
so beets internals never leak past the adapter boundary. No beets imports here.
"""

from enum import StrEnum

from pydantic import BaseModel


class Recommendation(StrEnum):
    """Mirror of beets' Recommendation enum (beets/autotag/match.py).

    Beets uses an IntEnum (none=0..strong=3); we expose the names as strings so
    the JSON contract is self-describing for the frontend.
    """

    none = "none"
    low = "low"
    medium = "medium"
    strong = "strong"


class TrackChangeStatus(StrEnum):
    """How a matched track row differs from the current file."""

    unchanged = "unchanged"
    changed = "changed"


class AlbumChange(BaseModel):
    """Album-level identity fields for one side of the before/after diff.

    Built for both the current files (``album_before``) and the matched release
    (``album_after``); the UI marks the fields named in ``Candidate.changed_fields``.
    """

    artist: str | None
    album: str | None
    year: int | None
    label: str | None
    country: str | None
    media: str | None


class TrackChange(BaseModel):
    """One matched track row, current (``*_before``) vs proposed (``*_after``)."""

    index: int | None
    status: TrackChangeStatus
    title_before: str | None
    title_after: str | None
    track_before: int | None
    track_after: int | None


class MissingTrack(BaseModel):
    """A track present on the matched release but absent from the folder.

    Maps from ``AlbumMatch.extra_tracks`` (beets' name for release-only tracks).
    """

    index: int | None
    title: str | None


class UnmatchedItem(BaseModel):
    """A local file with no counterpart on the matched release.

    Maps from ``AlbumMatch.extra_items`` (beets' name for folder-only files).
    """

    title: str | None
    track: int | None


class CandidateOption(BaseModel):
    """A ranked alternative release from ``task.candidates``.

    The switcher in the review screen lists these; ``index`` is the position in
    the beets candidate list and is what a choice references.
    """

    index: int
    confidence: float
    data_source: str | None
    disambiguation: str | None


class Candidate(BaseModel):
    """The full mapped payload for the top match of one album.

    Everything the review screen renders: confidence/recommendation, the album
    before/after diff, the per-track diff, missing/unmatched tracks, and the
    ranked alternative releases.
    """

    confidence: float
    recommendation: Recommendation
    data_source: str | None
    data_url: str | None
    changed_fields: list[str]
    album_before: AlbumChange
    album_after: AlbumChange
    tracks: list[TrackChange]
    missing: list[MissingTrack]
    unmatched: list[UnmatchedItem]
    options: list[CandidateOption]


class ParkedAlbum(BaseModel):
    """An album whose match is uncertain and is waiting for a user decision.

    Pushed onto the import bridge's out-queue; ``album_index`` keys the reply.
    """

    album_index: int
    folder: str
    candidate: Candidate


class ImportAction(StrEnum):
    """The decisions the user can return for a parked album.

    Subset of beets' choices relevant to chunk 1 (enter-id / search-again are a
    later chunk). ``apply`` selects a ranked option by index; ``abort`` stops the
    whole import (the session raises beets' ``ImportAbortError``, which beets'
    ``run()`` catches to stop cleanly).
    """

    apply = "apply"
    skip = "skip"
    asis = "asis"
    astracks = "astracks"
    abort = "abort"


class ImportChoice(BaseModel):
    """A user's decision for one parked album, pushed back over the bridge."""

    action: ImportAction
    # Index into Candidate.options; only meaningful when action == apply.
    # None means "apply the top candidate" — the session resolves None -> 0.
    candidate_index: int | None = None
