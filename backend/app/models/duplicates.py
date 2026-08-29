"""Typed contract for the Duplicate Albums (Find & Resolve) feature.

Lives outside ``app/beets/`` deliberately: it must not ``import beets``. The
adapter at ``app/beets/duplicates.py`` builds these models from beets objects.
``DuplicateAlbum`` subclasses the shared :class:`~app.models.album.Album` so the
core album fields (and the FE cover thumbnail keyed on ``id``) stay
interchangeable with the rest of the app, and adds the per-copy fields that
drive the keep/delete decision.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.models.album import Album


class DuplicateMode(StrEnum):
    """How albums are grouped into duplicate sets.

    ``strict`` — group by MusicBrainz album id only (beets album-mode default,
    ``beetsplug/duplicates.py`` default keys ``["mb_albumid"]``).
    ``fuzzy``  — MB album id when present, else a normalized ``albumartist`` +
    ``album`` key (catches untagged copies). MusicDrop extension layered on
    beets' same grouping algorithm.
    """

    strict = "strict"
    fuzzy = "fuzzy"


class DuplicateAlbum(Album):
    """A library album that is one copy in a duplicate group.

    Inherits ``id``, ``album_artist``, ``title``, ``year``, ``track_count``,
    ``genre`` from :class:`Album`; adds the fields the comparison table shows.
    """

    format: str | None
    bitrate_kbps: int | None
    folder: str
    is_suggested_keeper: bool


class DuplicateGroup(BaseModel):
    """A set of 2+ albums detected as duplicates.

    ``members`` are ordered keeper-first (beets ``_order`` heuristic: most
    tracks). ``suggested_keeper_id`` echoes ``members[0].id`` for convenience.
    """

    match_reason: str
    suggested_keeper_id: int
    members: list[DuplicateAlbum]


class DuplicatesReport(BaseModel):
    mode: DuplicateMode
    group_count: int
    album_count: int
    groups: list[DuplicateGroup]


class ResolveRequest(BaseModel):
    """Body of ``POST /api/duplicates/resolve``.

    ``mode`` is echoed so the server re-verifies the group with the same
    detection the client saw (no acting on stale UI state).
    """

    mode: DuplicateMode
    keep_album_id: int
    remove_album_ids: list[int] = Field(min_length=1)


class MovedAlbum(BaseModel):
    id: int
    album_artist: str
    title: str
    trash_path: str


class ResolveResult(BaseModel):
    kept_album_id: int
    moved: list[MovedAlbum]
    # Playlists whose `.m3u8` export was rewritten because a loser album this
    # resolve moved to Trash held one of their tracks — without it the export
    # keeps pointing at a file that is no longer where it says. Best-effort (a
    # failed write still counts). Defaulted: the adapter builds the result before
    # the collateral runs, and the endpoint fills it in.
    playlists_reexported: int = 0


class GroupDecision(BaseModel):
    """One group's keep/remove decision in a batch resolve.

    Mirrors :class:`ResolveRequest` minus the shared ``mode`` — the server
    re-verifies each group with the request's ``mode`` (no acting on stale UI).
    """

    keep_album_id: int
    remove_album_ids: list[int] = Field(min_length=1)


class ResolveAllRequest(BaseModel):
    """Body of ``POST /api/duplicates/resolve-all`` — resolve many groups at once."""

    mode: DuplicateMode
    groups: list[GroupDecision] = Field(min_length=1)


class SkippedGroup(BaseModel):
    """A group skipped in a batch because it drifted since the report
    (``StaleGroupError``). ``keep_album_id`` identifies which one for the UI."""

    keep_album_id: int
    reason: str


class ResolveAllResult(BaseModel):
    """Result of ``POST /api/duplicates/resolve-all``.

    ``group_count``/``moved_count`` are convenience totals for the summary line
    (groups resolved, copies moved to Trash).
    """

    resolved: list[ResolveResult]
    skipped_stale: list[SkippedGroup]
    group_count: int
    moved_count: int
    # Playlists re-exported for the batch as a WHOLE — one pass over the union of
    # every group's dropped items, so it is NOT the sum of the per-group numbers
    # (which stay 0 here: a playlist touched by two groups would be counted twice).
    playlists_reexported: int = 0
