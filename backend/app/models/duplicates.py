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
