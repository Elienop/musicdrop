from pydantic import BaseModel


class Album(BaseModel):
    id: int
    album_artist: str
    title: str
    year: int | None
    track_count: int
    genre: str | None
    mb_albumid: str | None


class AlbumPage(BaseModel):
    items: list[Album]
    total: int
    limit: int
    offset: int


class Track(BaseModel):
    id: int
    title: str
    track: int
    disc: int
    duration_seconds: float | None
    artist: str
    mb_trackid: str | None
    has_lyrics: bool
    format: str | None = None


class ReleaseIdentity(BaseModel):
    """Which release an album is — the provenance needed to tell two copies apart.

    ``data_source`` is the matcher (``"MusicBrainz"``/``"Deezer"``/...); ``media``
    + ``country`` + ``disambiguation`` are the edition; ``release_url`` links to
    the release page (``None`` when it can't be built). Every field is optional —
    an as-is or sparsely-tagged album may carry none of them.
    """

    data_source: str | None
    label: str | None
    country: str | None
    media: str | None
    disambiguation: str | None
    release_url: str | None


class AlbumDetail(Album):
    tracks: list[Track]
    release: ReleaseIdentity | None = None
