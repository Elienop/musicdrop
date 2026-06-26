from pydantic import BaseModel, HttpUrl


class Artist(BaseModel):
    name: str
    album_count: int


class ArtistImageSettings(BaseModel):
    """The artist-image feature on/off flag (GET + PUT body + PUT response)."""

    enabled: bool


class ArtistImageOverrideResult(BaseModel):
    """Outcome of pinning a manual artist-image override."""

    ok: bool
    content_type: str


class ArtistImageUrlOverride(BaseModel):
    """Request body for setting an artist override from an image URL."""

    url: HttpUrl
