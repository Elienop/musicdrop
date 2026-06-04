"""Shared HTTP image download + validation for the artist-image sources.

GET a URL, reject oversize/non-image responses, return the bytes + content-type.
Every source (Deezer, Spotify, fanart.tv) downloads its chosen image the same
way, so the validation lives here once.
"""

from __future__ import annotations

import httpx

from app.artwork.images import MAX_IMAGE_BYTES
from app.artwork.source import ResolvedImage, TransientSourceError


async def download_image(client: httpx.AsyncClient, url: str) -> ResolvedImage:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TransientSourceError(f"image download failed: {exc}") from exc

    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise TransientSourceError("image exceeds size limit (Content-Length)")

    content_type = response.headers.get("content-type", "")
    if not content_type.lower().startswith("image/"):
        raise TransientSourceError(f"download was not an image: {content_type!r}")

    data = response.content
    if len(data) > MAX_IMAGE_BYTES:
        raise TransientSourceError("image exceeds size limit")

    return ResolvedImage(data=data, content_type=content_type)
