"""Generic image validation shared by the cover + artist-image endpoints.

No beets here — magic-byte sniffing and the upload size cap are plain image
utilities, so they live OUTSIDE the beets adapter and can be imported by any
router without pulling beets in (CLAUDE.md rule 3).
"""

from __future__ import annotations

from typing import Final

# Hard cap on image bytes any upload endpoint will materialize (DoS guard).
MAX_IMAGE_BYTES: Final = 10 * 1024 * 1024

#: What every image path serves when it has no content-type it can legally send.
FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"


def is_header_safe_content_type(value: str) -> bool:
    """Whether ``value`` can be sent as a Content-Type header at all.

    Every content-type this app serves arrives from OUTSIDE it — a third-party
    CDN's response header, or a ``.mime``/``.src`` sidecar written from one —
    and reaches a response header untouched. Two shapes break that, and neither
    needs disk corruption to arrive:

    * **non-ASCII** (``image/日本語``): Starlette encodes header values as
      latin-1, so building the response raises ``UnicodeEncodeError`` and the
      request 500s. ``httpx`` decodes header bytes as UTF-8, so a value like
      this survives a ``startswith("image/")`` check on the download path and
      poisons the cache slot permanently.
    * **an embedded newline** (``image/png\\nX-Injected: yes``): a
      response-splitting attempt. Starlette passes it through and h11 refuses
      the frame, dropping the connection with no response at all.

    ``isprintable()`` is the part that rejects ``\\n``, ``\\r`` and ``\\x00``
    while still accepting a legitimately parameterised type
    (``image/svg+xml; charset=utf-8``). Empty is rejected too — an empty
    Content-Type makes browsers sniff the body.

    NOTE for whoever tests a caller of this: Starlette's TestClient never
    encodes response headers, so BOTH failures above return a green 200 through
    it. A TestClient assertion cannot see this class of bug; assert on the
    content-type value that gets served, and unit-test this predicate directly.
    """
    return bool(value) and value.isascii() and value.isprintable()


def sniff_image_mime(data: bytes) -> str | None:
    """Best-effort image mime from magic bytes; None if not a known image."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
