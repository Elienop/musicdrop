"""Shared HTTP revalidation for the binary image endpoints.

Both the artist portrait (``api.artists``) and the album cover (``api.albums``)
are served with a content-hash ``ETag`` and ``Cache-Control: no-cache`` so a
freshly-edited image shows up without a hard refresh, while an unchanged one
stays cheap — the browser revalidates and gets a bodiless ``304``.

The ``If-None-Match`` check follows RFC 9110's *weak* comparison: a reverse
proxy (e.g. nginx with gzip on) may weaken our strong ``"<hash>"`` to
``W/"<hash>"`` before the browser echoes it back, so we strip the optional
``W/`` prefix on both sides — otherwise the validator never matches and every
view re-downloads the full image, defeating the point.
"""

from __future__ import annotations

import hashlib

from fastapi import Request, Response


def _opaque(tag: str) -> str:
    """An entity-tag's opaque value: the weak ``W/`` prefix stripped (weak
    comparison treats ``W/"x"`` and ``"x"`` as the same tag)."""
    tag = tag.strip()
    return tag[2:] if tag.startswith("W/") else tag


def if_none_match_hit(request: Request, etag: str) -> bool:
    """True when the request's ``If-None-Match`` already holds ``etag`` (RFC 9110
    weak comparison; ``*`` matches any existing entity). Lets an endpoint answer a
    ``304`` from a cheap validator without materializing the body first."""
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is None:
        return False
    candidates = {_opaque(t) for t in if_none_match.split(",")}
    return "*" in candidates or _opaque(etag) in candidates


def not_modified(etag: str) -> Response:
    """A bodiless ``304`` carrying the revalidation headers for ``etag``."""
    return Response(status_code=304, headers={"Cache-Control": "no-cache", "ETag": etag})


def image_response(image_bytes: bytes, mime: str, etag: str) -> Response:
    """A ``200`` image response with ``etag`` + the revalidation headers."""
    return Response(
        content=image_bytes,
        media_type=mime,
        headers={"Cache-Control": "no-cache", "ETag": etag},
    )


def revalidating_image_response(request: Request, image_bytes: bytes, mime: str) -> Response:
    """A revalidating image ``Response``: a content-hash ``ETag`` plus
    ``Cache-Control: no-cache``. Returns a bodiless ``304`` when the request's
    ``If-None-Match`` already holds the current entity-tag (weak comparison;
    ``*`` matches any existing entity)."""
    etag = f'"{hashlib.sha256(image_bytes).hexdigest()}"'
    if if_none_match_hit(request, etag):
        return not_modified(etag)
    return image_response(image_bytes, mime, etag)
