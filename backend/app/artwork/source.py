"""The swappable artist-image source contract.

A source takes a plain artist-name ``str`` and returns the resolved portrait
bytes (or ``None`` when it can't confidently identify the artist). Deezer is
the only implementation in this chunk; MusicBrainz/fanart.tv/Spotify can be
added later behind the same Protocol.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ResolvedImage:
    """A resolved artist portrait: raw image bytes plus its content-type."""

    data: bytes
    content_type: str


class TransientSourceError(Exception):
    """A source failure that should be retried sooner than a confirmed no-match.

    Raised for HTTP errors (incl. 429), timeouts/connect errors, malformed or
    non-JSON bodies, and unusable downloads (oversize / non-image). The service
    negative-caches these with a SHORT TTL. A confirmed no-verified-match is the
    distinct case where ``resolve`` returns ``None`` (long TTL).
    """


@runtime_checkable
class ArtistImageSource(Protocol):
    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        """Resolve ``name`` (optionally aided by a MusicBrainz ``mbid``) to a portrait.

        Returns ``None`` ONLY for a confirmed no-verified-match. Raises
        :class:`TransientSourceError` for any transient failure.
        """
        ...


class ArtistImageSourceBase(ABC):
    """Concrete base for the named artist-image sources (Deezer, fanart.tv, Spotify).

    ``resolve`` declares BOTH identifiers — the human ``name`` and the MusicBrainz
    ``mbid`` — because callers pass them uniformly and source picking is
    deterministic (a caller cannot know which one each source needs). Each concrete
    source may therefore ignore the identifier it cannot use: fanart.tv keys
    purely off ``mbid`` (ignores ``name``), while Deezer and Spotify search by
    ``name`` (ignore ``mbid``). Overriding :meth:`resolve` is exempt from the
    unused-argument rule (python:S1172) on exactly this account.
    """

    @abstractmethod
    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        """Resolve ``name`` (optionally aided by a MusicBrainz ``mbid``) to a portrait.

        Returns ``None`` ONLY for a confirmed no-verified-match. Raises
        :class:`TransientSourceError` for any transient failure. An implementation
        may ignore either identifier it cannot use.
        """
        ...
