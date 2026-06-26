"""Deezer artist-image source with strict name verification.

Flow for ``resolve(name)``:

1. ``GET /search/artist?q=<name>&limit=<N>`` (no API key).
2. **Verify**: keep only hits whose name, after :func:`normalize_artist_name`,
   *equals* the normalized query. This is the safety rail — "Beatles" never
   matches "The Beatles". Near matches are dropped, not fuzzily accepted.
3. **Pick**: among verified hits, the most prominent by ``nb_fan`` then
   ``nb_album`` (the canonical artist, not a tribute act).
4. Download the chosen ``picture_xl`` (fallback ``picture_big``), guard its
   size + content-type, and return its bytes + content-type.

Outcome contract (see :class:`TransientSourceError`):

* **Confirmed no-match** (a valid search with no verified-name hit, or a
  verified hit with no usable picture URL) -> return ``None`` (long negative
  TTL).
* **Transient failure** (HTTP error incl. 429, timeout/connect error, non-JSON
  or malformed body, oversize or non-image download) -> raise
  ``TransientSourceError`` (short negative TTL). Never returns the wrong artist.
"""

from typing import Any  # Deezer JSON is untyped; we validate it structurally.

import httpx

from app.artwork.download import download_image
from app.artwork.normalize import normalize_artist_name
from app.artwork.source import ResolvedImage, TransientSourceError

_SEARCH_URL = "https://api.deezer.com/search/artist"

# Deezer serves a no-photo artist's picture URL as a 302 to this blank-avatar
# placeholder (the hash is the MD5 of the empty string). Treat it as no-match.
_NO_PHOTO_PLACEHOLDER = "d41d8cd98f00b204e9800998ecf8427e"


class DeezerArtistImageSource:
    def __init__(self, *, client: httpx.AsyncClient, search_limit: int) -> None:
        self._client = client
        self._search_limit = search_limit

    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        hits = await self._search(name)

        target = normalize_artist_name(name)
        if not target:
            return None

        verified = [
            h
            for h in hits
            # Skip non-str names so a JSON null can't coerce via str(None).
            if isinstance(h.get("name"), str) and normalize_artist_name(h["name"]) == target
        ]
        if not verified:
            return None

        chosen = max(verified, key=self._prominence)
        url = self._picture_url(chosen)
        if not url:
            return None

        return await download_image(
            self._client, url, reject_url_substrings=(_NO_PHOTO_PLACEHOLDER,)
        )

    async def _search(self, name: str) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(
                _SEARCH_URL, params={"q": name, "limit": self._search_limit}
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # httpx.HTTPError covers status/timeout/connect errors; ValueError
            # covers a non-JSON body. All transient.
            raise TransientSourceError(f"Deezer search failed: {exc}") from exc
        # payload is untyped JSON; validate the shape structurally.
        if not isinstance(payload, dict):
            raise TransientSourceError("Deezer search payload was not an object")
        data = payload.get("data")
        if not isinstance(data, list):
            raise TransientSourceError("Deezer search payload had no 'data' list")
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _prominence(hit: dict[str, Any]) -> tuple[int, int]:
        return (_as_int(hit.get("nb_fan")), _as_int(hit.get("nb_album")))

    @staticmethod
    def _picture_url(hit: dict[str, Any]) -> str:
        xl = hit.get("picture_xl")
        if isinstance(xl, str) and xl:
            return xl
        big = hit.get("picture_big")
        if isinstance(big, str) and big:
            return big
        return ""


def _as_int(value: Any) -> int:  # value is untyped Deezer JSON.
    return value if isinstance(value, int) else 0
