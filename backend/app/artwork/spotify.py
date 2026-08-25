"""Spotify artist-image source (name search via client-credentials OAuth).

Manages its own access token (client-credentials has no refresh token, so it
re-requests on expiry or a 401). Searches artists by name, strictly name-verifies
the hit (same rail as Deezer), and downloads the widest image. Spotify JSON is
untyped, so it is validated structurally.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from app.artwork.download import download_image
from app.artwork.normalize import normalize_artist_name
from app.artwork.source import ArtistImageSourceBase, ResolvedImage, TransientSourceError

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"
_TOKEN_SKEW = 60.0  # refresh this many seconds before the stated expiry


class SpotifyArtistImageSource(ArtistImageSourceBase):
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        client_id: str,
        client_secret: str,
        search_limit: int = 5,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._client_id = client_id
        self._client_secret = client_secret
        self._search_limit = min(search_limit, 10)  # Spotify search caps at 10
        self._now = now
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def resolve(self, name: str, *, mbid: str | None = None) -> ResolvedImage | None:
        token = await self._access_token()
        try:
            response = await self._search(name, token)
            if response.status_code == 401:
                token = await self._access_token(force=True)
                response = await self._search(name, token)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TransientSourceError(f"Spotify search failed: {exc}") from exc

        target = normalize_artist_name(name)
        if not target:
            return None
        items = _items(payload)
        verified = [
            a
            for a in items
            if isinstance(a.get("name"), str) and normalize_artist_name(a["name"]) == target
        ]
        if not verified:
            return None
        chosen = max(verified, key=_prominence)
        url = _first_image_url(chosen)
        if not url:
            return None
        return await download_image(self._client, url)

    async def _search(self, name: str, token: str) -> httpx.Response:
        return await self._client.get(
            _SEARCH_URL,
            params={"q": name, "type": "artist", "limit": self._search_limit},
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _access_token(self, *, force: bool = False) -> str:
        now = self._now()
        if not force and self._token is not None and now < self._token_expiry:
            return self._token
        try:
            response = await self._client.post(
                _TOKEN_URL,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TransientSourceError(f"Spotify token request failed: {exc}") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise TransientSourceError("Spotify token response missing access_token")
        expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        self._token = token
        self._token_expiry = now + ttl - _TOKEN_SKEW
        return token


def _items(payload: object) -> list[dict[str, Any]]:
    artists = payload.get("artists") if isinstance(payload, dict) else None
    items = artists.get("items") if isinstance(artists, dict) else None
    if not isinstance(items, list):
        return []
    return [a for a in items if isinstance(a, dict)]


def _prominence(artist: dict[str, Any]) -> tuple[int, int]:
    pop = artist.get("popularity")
    followers = artist.get("followers")
    total = followers.get("total") if isinstance(followers, dict) else None
    return (pop if isinstance(pop, int) else 0, total if isinstance(total, int) else 0)


def _first_image_url(artist: dict[str, Any]) -> str:
    images = artist.get("images")
    if not isinstance(images, list) or not images:
        return ""
    first = images[0]
    url = first.get("url") if isinstance(first, dict) else None
    return url if isinstance(url, str) else ""
