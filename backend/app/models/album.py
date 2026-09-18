from pydantic import BaseModel, Field


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
    # Mutually exclusive with has_lyrics: a track with real lyrics is never
    # reported instrumental, even if a stale flag says so (see _to_track).
    instrumental: bool
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
    # ONE field, not a flag plus a folder: two cannot then disagree, and the
    # sentence needs the folder anyway. The fact, not a cause — an import that
    # stopped mid-placement produces it (measured, test_import_incremental_e2e),
    # and so do an in_place import, an edited ``directory:`` and rows left in a
    # Trash outside the music folder.
    folder_outside_library: str | None = Field(
        description="The folder of one album file that is not under the library folder.",
    )
