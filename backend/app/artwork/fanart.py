"""fanart.tv artist-image source (MusicBrainz MBID-keyed).

GET /v3/music/{mbid} with the keys as HTTP headers (api-key + optional
client-key), take the highest-``likes`` ``artistthumb``, and download it. This is
an MBID-only source: it returns ``None`` when no mbid is supplied. A 404 means no
art is catalogued (``None``); anything else is transient. fanart.tv JSON is
untyped, so it is validated structurally.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.artwork.download import download_image
from app.artwork.source import ResolvedImage, TransientSourceError

_BASE_URL = "https://webservice.fanart.tv/v3/music"


class FanartTvArtistImageSource:
    def __init__(self, *, client: httpx.AsyncClient, api_key: str, client_key: str = "") -> None:
        self._client = client
        self._api_key = api_key
        self._client_key = client_key

    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        if not mbid:
            return None
        data = await self._fetch_artist_json(mbid)
        if data is None:
            return None
        url = _best_thumb_url(data)
        if not url:
            return None
        return await download_image(self._client, url)

    async def _fetch_artist_json(self, mbid: str) -> dict[str, Any] | None:
        headers = {"api-key": self._api_key}
        if self._client_key:
            headers["client-key"] = self._client_key
        try:
            response = await self._client.get(f"{_BASE_URL}/{mbid}", headers=headers)
        except httpx.HTTPError as exc:
            raise TransientSourceError(f"fanart.tv request failed: {exc}") from exc
        if response.status_code == 404:
            return None  # no art catalogued for this artist
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TransientSourceError(f"fanart.tv error: {exc}") from exc
        if not isinstance(payload, dict):
            raise TransientSourceError("fanart.tv payload was not an object")
        return payload


def _best_thumb_url(data: dict[str, Any]) -> str:
    thumbs = data.get("artistthumb")
    if not isinstance(thumbs, list):
        return ""
    candidates = [
        t for t in thumbs if isinstance(t, dict) and isinstance(t.get("url"), str) and t["url"]
    ]
    if not candidates:
        return ""
    best = max(candidates, key=_likes)
    url = best.get("url")
    return url if isinstance(url, str) else ""


def _likes(thumb: dict[str, Any]) -> int:
    # fanart.tv "likes" is a STRING in the JSON; isinstance-narrow before int().
    raw = thumb.get("likes")
    if not isinstance(raw, (str, int)):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
