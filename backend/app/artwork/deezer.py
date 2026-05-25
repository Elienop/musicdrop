"""Deezer artist-image source with strict name verification.

Flow for ``resolve(name)``:

1. ``GET /search/artist?q=<name>&limit=<N>`` (no API key).
2. **Verify**: keep only hits whose name, after :func:`normalize_artist_name`,
   *equals* the normalized query. This is the safety rail — "Beatles" never
   matches "The Beatles". Near matches are dropped, not fuzzily accepted.
3. **Pick**: among verified hits, the most prominent by ``nb_fan`` then
   ``nb_album`` (the canonical artist, not a tribute act).
4. Download the chosen ``picture_xl`` (fallback ``picture_big``) and return
   its bytes + content-type.

Any HTTP/network/parse failure (incl. 429) yields ``None`` — the caller
negative-caches and the UI shows the glyph. Never returns the wrong artist.
"""

from typing import Any

import httpx

from app.artwork.normalize import normalize_artist_name
from app.artwork.source import ResolvedImage

_SEARCH_URL = "https://api.deezer.com/search/artist"


class DeezerArtistImageSource:
    def __init__(self, *, client: httpx.AsyncClient, search_limit: int) -> None:
        self._client = client
        self._search_limit = search_limit

    async def resolve(self, name: str) -> ResolvedImage | None:
        hits = await self._search(name)
        if hits is None:
            return None

        target = normalize_artist_name(name)
        if not target:
            return None

        verified = [h for h in hits if normalize_artist_name(str(h.get("name", ""))) == target]
        if not verified:
            return None

        chosen = max(verified, key=self._prominence)
        url = self._picture_url(chosen)
        if not url:
            return None

        return await self._download(url)

    async def _search(self, name: str) -> list[dict[str, Any]] | None:
        try:
            response = await self._client.get(
                _SEARCH_URL, params={"q": name, "limit": self._search_limit}
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # ValueError covers a non-JSON / malformed body.
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return None
        return [item for item in data if isinstance(item, dict)]

    async def _download(self, url: str) -> ResolvedImage | None:
        try:
            response = await self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        content_type = response.headers.get("content-type", "application/octet-stream")
        return ResolvedImage(data=response.content, content_type=content_type)

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


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) else 0
