"""Build an album's release identity (provenance) for the contract.

Read-only: reads ``data_source`` (a beets flex attr) + the fixed
``label``/``country``/``media``/``albumdisambig`` fields via ``getattr`` so the
same helper serves a beets ``Album`` (library detail) and a match ``AlbumInfo``
(the duplicate prompt's incoming side). Builds the release page URL from the
source + id. No network.
"""

from __future__ import annotations

from typing import Any

from app.models.album import ReleaseIdentity


def _opt_str(value: object) -> str | None:
    """Coerce to a stripped string, or ``None`` (beets fixed fields are "" when unset)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _release_url(data_source: str | None, release_id: str | None) -> str | None:
    if not data_source or not release_id:
        return None
    src = data_source.strip().lower()
    if src == "musicbrainz":
        return f"https://musicbrainz.org/release/{release_id}"
    if src == "deezer":
        return f"https://www.deezer.com/album/{release_id}"
    return None  # source has no known release-page URL scheme


def release_identity(obj: Any, release_id: object) -> ReleaseIdentity:
    """Map a beets ``Album`` or a match ``AlbumInfo`` to a ``ReleaseIdentity``.

    ``release_id`` is the source's release id (``Album.mb_albumid`` /
    ``AlbumInfo.album_id``) — passed explicitly since the two name it differently.
    """
    data_source = _opt_str(getattr(obj, "data_source", None))
    return ReleaseIdentity(
        data_source=data_source,
        label=_opt_str(getattr(obj, "label", None)),
        country=_opt_str(getattr(obj, "country", None)),
        media=_opt_str(getattr(obj, "media", None)),
        disambiguation=_opt_str(getattr(obj, "albumdisambig", None)),
        release_url=_release_url(data_source, _opt_str(release_id)),
    )
