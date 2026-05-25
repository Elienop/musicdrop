from pydantic import BaseModel


class Album(BaseModel):
    id: int
    album_artist: str
    title: str
    year: int | None
    track_count: int
    genre: str | None


class AlbumPage(BaseModel):
    items: list[Album]
    total: int
    limit: int
    offset: int
