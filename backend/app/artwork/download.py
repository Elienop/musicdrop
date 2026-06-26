"""Shared HTTP image download + validation for the artist-image sources.

GET a URL (following redirects), reject oversize/non-image responses, return the
bytes + content-type. Every source (Deezer, Spotify, fanart.tv) downloads its
chosen image the same way, so the validation lives here once. A source may pass
``reject_url_substrings`` to treat a known "no image" placeholder — e.g. Deezer
302-redirects a no-photo artist to its blank-avatar URL — as a confirmed
no-match (``None``) rather than a transient failure.
"""

from __future__ import annotations

import httpx

from app.artwork.images import MAX_IMAGE_BYTES
from app.artwork.source import ResolvedImage, TransientSourceError


async def download_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    reject_url_substrings: tuple[str, ...] = (),
) -> ResolvedImage | None:
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TransientSourceError(f"image download failed: {exc}") from exc

    # A known "no image" placeholder is a confirmed no-match, not a failure: e.g.
    # Deezer 302-redirects a no-photo artist to its blank-avatar URL (hash = MD5
    # of the empty string), so the final URL — not the original — carries the tell.
    final_url = str(response.url)
    if any(token in final_url for token in reject_url_substrings):
        return None

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


async def fetch_image_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    """Fetch a USER-supplied image URL as raw bytes (follows redirects, size-capped).

    Distinct from ``download_image`` (trusted source CDNs, validated by Content-Type
    header): a pasted URL may carry a wrong/missing content-type, so this returns raw
    bytes and the caller validates them with ``sniff_image_mime`` (magic bytes), exactly
    how the upload endpoint validates an uploaded file. Raises ``ValueError`` on a
    non-fetchable or oversize response.
    """
    try:
        response = await client.get(url, follow_redirects=True, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ValueError(f"could not fetch image: {exc}") from exc
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise ValueError("image is too large (max 10 MB)")
    data = response.content
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image is too large (max 10 MB)")
    return data
