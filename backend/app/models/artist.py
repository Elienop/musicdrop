from typing import Literal

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


#: The artwork sources a user may fetch a portrait from, by stable id. These ids
#: mirror app/artwork/factory.py's FANARTTV/SPOTIFY/DEEZER constants and are the
#: wire contract - the generated TS turns them into a union, so a rename here is
#: a breaking change. The order is the chain's fallback order; the sources
#: endpoint is ordered and the UI renders it in that order.
#: tests/test_artist_models.py pins both the pair and the order.
ArtistImageSourceId = Literal["fanarttv", "spotify", "deezer"]


class ArtistImageSourceOption(BaseModel):
    """One source the UI may offer for ONE artist.

    ``available`` is per-artist, not per-install: fanart.tv is MBID-keyed and
    returns nothing without one, so it can be fully configured yet unusable for
    a particular artist. ``reason`` carries the sentence the UI shows in that
    case (and is ``None`` when the source is usable). Sources whose credentials
    are unset are OMITTED from the list entirely rather than sent unavailable -
    they are an install-level fact, not something the user can act on here.

    Every field is required (no Pydantic defaults) so the generated TypeScript
    types them as present rather than optional.
    """

    id: ArtistImageSourceId
    label: str
    available: bool
    reason: str | None


class ArtistImageSourceList(BaseModel):
    """The sources offered for one artist, in the chain's own fallback order."""

    sources: list[ArtistImageSourceOption]


class ArtistImageResetResult(BaseModel):
    """What a reset actually did - the whole point of the endpoint.

    ``cleared_override`` and ``cleared_auto`` are reported separately because
    the two slots are independent and either may have been absent (or, on an
    unwritable cache dir, may have refused to go). The UI says what happened
    instead of implying a re-fetch that did not occur.
    """

    ok: bool
    cleared_override: bool
    cleared_auto: bool
