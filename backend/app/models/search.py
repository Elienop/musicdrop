from typing import Literal

from pydantic import BaseModel

from app.models.album import Album
from app.models.artist import Artist

# The three searchable entity types — the wire values of /api/search's `type`
# query param (typed "View all" mode).
SearchEntity = Literal["artists", "albums", "tracks"]


class SearchTrack(BaseModel):
    id: int
    title: str
    artist: str
    album: str
    # album_id lets a track row link to its album page and is the seam the
    # later playlist feature uses to add a track without re-fetching. None for
    # tracks beets has not grouped under an album (singletons).
    album_id: int | None
    duration_seconds: float | None


class SearchResults(BaseModel):
    artists: list[Artist]
    albums: list[Album]
    tracks: list[SearchTrack]
    # Per-type totals power "N of M" when the lists are capped at `limit`.
    artist_total: int
    album_total: int
    track_total: int


class TypedSearchPage(BaseModel):
    """One entity of the search, paged — the "View all N" page contract.

    Returned by GET /api/search when `type` is present. ONLY the section named
    by ``type`` is populated (the other two lists stay empty); ``total`` is
    that entity's FULL match count, and ``limit``/``offset`` echo the request
    so the FE can page statelessly from URL state. Ordering matches the
    sectioned mode (same underlying queries), so "View all" page 1 lines up
    with the section preview. ``SearchResults`` is deliberately untouched —
    the default (no-``type``) response stays byte-identical.
    """

    type: SearchEntity
    artists: list[Artist] = []
    albums: list[Album] = []
    tracks: list[SearchTrack] = []
    total: int
    limit: int
    offset: int
