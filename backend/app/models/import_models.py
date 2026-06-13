"""Pydantic contract for the beets import flow.

These models are the stable surface the API/UI will consume. They are mapped
from beets' AlbumMatch/Distance/AlbumInfo/TrackInfo in app/beets/import_mapping.py
so beets internals never leak past the adapter boundary. No beets imports here.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

ImportOrigin = Literal["manual", "inbox", "sweep", "bank_apply"]


class ImportOptions(BaseModel):
    """Per-import overrides (replaces the reserved ``dict[str, str]``).

    ``operation`` ``"default"`` falls through to the user's beets config (the
    manual-import default). ``"move"``/``"copy"`` force that operation for this
    import only. ``unattended`` ``True`` is the inbox path: no human review —
    uncertain/duplicate albums are set aside rather than parked. ``sweep``
    ``True`` is the banking sweep: an unattended, beets-incremental run that
    BANKS every set-aside album (with its candidate payload) instead of just
    skipping it, recorded as ``origin="sweep"``. A sweep is unattended by
    definition — the session enforces ``unattended or sweep`` — so
    ``{"sweep": true}`` alone is a complete sweep request. The sweep forces no
    file operation: ``operation`` behaves exactly as for a manual import (the
    in-library guard still force-corrects in-library sources to move).
    """

    operation: Literal["default", "move", "copy"] = "default"
    unattended: bool = False
    sweep: bool = False


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
    the beets candidate list and is what a choice references. ``release_id`` is
    the metadata backend's id for the release (``AlbumInfo.album_id`` — an
    MBID for MusicBrainz, the deezer id for Deezer): the bank's apply runner
    pins ``import.search_ids`` to it so the apply imports exactly the release
    the user chose. None for sources without an id (the apply then falls back
    to an unpinned lookup's top candidate).
    """

    index: int
    confidence: float
    data_source: str | None
    disambiguation: str | None
    release_id: str | None = None
    # This option's own matched-release identity — mirrors ``album_after`` but
    # per candidate, so the bank's up-front duplicate check keys on the SELECTED
    # release (not the top match) and equals what the apply does. Optional +
    # defaulting to None keeps rows banked before these fields existed valid;
    # the endpoint falls back to ``album_after`` field-by-field when absent.
    album_artist: str | None = None
    album: str | None = None
    year: int | None = None


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
    # The matched release's Cover Art Archive front-image URL (MusicBrainz only),
    # or None. The browser fetches it directly and falls back to a placeholder on
    # error - so a release with no CAA art degrades gracefully.
    cover_after_url: str | None
    # Whether the current files carry embedded cover art. Drives the "+ cover art"
    # change chip and whether the "before" panel attempts to load an image.
    has_current_art: bool
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


class AlbumOutcomeStatus(StrEnum):
    """What the worker did with one album, for the live import feed.

    applied              -> a strong match auto-applied (beets applied it inline)
    needs_review         -> an uncertain match was parked and awaits a decision
    needs_dup_resolution -> the album duplicates one already in the library and
                            awaits the user's skip/keep/replace/merge decision
    skipped              -> nothing to apply (no candidates), so it was skipped
    """

    applied = "applied"
    needs_review = "needs_review"
    needs_dup_resolution = "needs_dup_resolution"
    skipped = "skipped"


class AlbumOutcome(BaseModel):
    """A compact per-album record the worker emits for every album it processes.

    Pushed onto the import bridge's non-blocking outcome channel so the API can
    render the live feed (auto-applied + skipped + the current parked album) and
    a truthful summary. The full Candidate (for the review screen) travels
    separately on the parked album; this stays small on purpose.
    """

    album_index: int
    folder: str
    artist: str | None
    album: str | None
    recommendation: Recommendation
    confidence: float
    status: AlbumOutcomeStatus
    # The beets library album id, attached by a FOLLOW-UP outcome (same
    # album_index) once beets' task.add() has run — choose_match emits the
    # original outcome BEFORE the album exists, so it is always None there.
    # Follow-ups force status=applied: by then the album IS in the library,
    # however it was chosen (strong auto-apply or a user apply/asis decision),
    # and a non-applied follow-up status could regress a decided feed row.
    album_id: int | None = None


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


class ExistingAlbum(BaseModel):
    """A slim view of one in-library album that the incoming import duplicates.

    Built from a beets ``Album`` (the ``found_duplicates`` set). ``album_id`` is
    the library id (so the FE can fetch its cover via ``/api/albums/{id}/cover``
    and so Replace can target it by stable id).
    """

    album_id: int
    album_artist: str | None
    album: str | None
    year: int | None
    track_count: int
    format: str | None
    bitrate_kbps: int | None
    folder: str


class IncomingAlbum(BaseModel):
    """A slim view of the album being imported — symmetric with ExistingAlbum.

    Built from the import task's current files (works for both an APPLY match and
    an ASIS import, neither of which is needed to render the duplicate decision).
    ``has_current_art`` drives whether the FE attempts the current-files cover.
    """

    album_artist: str | None
    album: str | None
    year: int | None
    track_count: int
    format: str | None
    bitrate_kbps: int | None
    folder: str
    has_current_art: bool


class DuplicateAction(StrEnum):
    """beets' four faithful duplicate-resolution actions (importer/stages.py).

    skip_new  -> don't import the new album (beets 's' -> Action.SKIP)
    keep_both -> import alongside the existing copy (beets 'k' -> no-op)
    replace   -> import the new album, move the existing copy to Trash (beets
                 'r' is a hard delete; we divert to the reversible Trash)
    merge     -> combine into one album; beets rebuilds + re-runs the match, so
                 it reappears as a normal candidate review (beets 'm')
    """

    skip_new = "skip_new"
    keep_both = "keep_both"
    # 'replace' shadows str.replace under StrEnum (str subclass); the member is a
    # plain enum value, so mypy's incompatible-override is a false positive here.
    replace = "replace"  # type: ignore[assignment]
    merge = "merge"


class DuplicatePrompt(BaseModel):
    """A parked import album that duplicates one or more already in the library.

    Pushed onto the import bridge's duplicate channel; ``album_index`` keys the
    reply (the SAME index the album's candidate outcome already carries).
    """

    album_index: int
    incoming: IncomingAlbum
    existing: list[ExistingAlbum]


class DuplicateDecision(BaseModel):
    """The user's resolution for a parked duplicate, pushed back over the bridge.

    No payload beyond the action: skip/keep/merge are global to the prompt and
    replace removes the whole ``existing`` set (beets resolves duplicates as a
    set). Per-existing selection is deferred (see the spec's out-of-scope).
    """

    action: DuplicateAction
