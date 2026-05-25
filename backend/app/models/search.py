from pydantic import BaseModel

from app.models.album import Album
from app.models.artist import Artist


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
