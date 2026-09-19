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


# The FACT, not a cause: an import stopped mid-placement produces it, and so do
# an in_place import and rows left in a Trash outside the music folder (all
# measured, test_album_outside_library). One object rather than two flat fields,
# which could disagree. Kept out of the docstring: that becomes the OpenAPI
# description, which is one sentence.
class OutsideLibrary(BaseModel):
    """Where an album file sits when it is not in the library folder."""

    folder: str = Field(
        description="The folder holding an album file that is not in the library folder.",
    )
    # The one shape where adding that folder again is measured safe under every
    # file operation: beets excludes the album from find_duplicates and
    # remove_replaced absorbs its rows, so no duplicate is asked and nothing
    # reaches Trash. A straddle or a multi-folder album gets the fact alone.
    holds_every_track: bool = Field(
        description="True when every track of the album is a file in that one folder.",
    )


class AlbumDetail(Album):
    tracks: list[Track]
    release: ReleaseIdentity | None = None
    outside_library: OutsideLibrary | None = Field(
        description="Set when some of the album's files are not in the library folder.",
    )
