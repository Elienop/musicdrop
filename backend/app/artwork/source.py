"""The swappable artist-image source contract.

A source takes a plain artist-name ``str`` and returns the resolved portrait
bytes (or ``None`` when it can't confidently identify the artist). Deezer is
the only implementation in this chunk; MusicBrainz/fanart.tv/Spotify can be
added later behind the same Protocol.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ResolvedImage:
    """A resolved artist portrait: raw image bytes plus its content-type."""

    data: bytes
    content_type: str


@runtime_checkable
class ArtistImageSource(Protocol):
    async def resolve(self, name: str) -> ResolvedImage | None:
        """Resolve ``name`` to a portrait, or ``None`` if nothing verifies."""
        ...
