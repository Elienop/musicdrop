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


def header_safe_content_type(value: str) -> str | None:
    """``value`` as it must actually be SENT, or ``None`` if it cannot be.

    Returns a value rather than a bool on purpose. A boolean predicate that
    normalises internally is a trap: it widens what it accepts while every
    caller keeps passing the ORIGINAL through to the wire, and no mutation of
    the predicate's own line can catch that — mutation-testing a predicate says
    nothing about what its callers do with a True. Using the RETURN VALUE closes
    both halves at once.

    Every content-type this app serves arrives from OUTSIDE it — a third-party
    CDN's response header, a ``.mime``/``.src`` sidecar written from one, or a
    media file's embedded picture MIME — and reaches a response header
    untouched. Three shapes break that, and none needs disk corruption:

    * **non-ASCII** (``image/日本語``): Starlette encodes header values as
      latin-1, so building the response raises ``UnicodeEncodeError`` and the
      request 500s. ``httpx`` decodes header bytes as UTF-8, so a value like
      this survives a ``startswith("image/")`` check on the download path and
      poisons the cache slot permanently.
    * **an embedded newline** (``image/png\\nX-Injected: yes``): a
      response-splitting attempt. Starlette passes it through and h11 refuses
      the frame, dropping the connection with no response at all.
    * **any ASCII control character, anywhere** (``image/png\\n``,
      ``image/png\\t``): h11 fullmatches a field value against
      ``([^\\x00\\s]+(?:[ \\t]+[^\\x00\\s]+)*)?``, so a leading, trailing or
      embedded control byte is an illegal header. Measured against a real
      server, ``image/png\\n`` and ``image/png\\r`` give "Empty reply from
      server" under BOTH the h11 and httptools workers.
    * **empty, whitespace-only, or padded**: an empty Content-Type is legal on
      the wire but makes the browser sniff the body, which is what a declared
      type exists to prevent. Padding is subtler — ``" image/png "`` is a
      dropped connection under the h11 worker (httptools tolerates it), so it
      is TRIMMED rather than rejected: the type is good, only the framing is
      not.

    ``isprintable()`` is tested on the RAW value, which is what rejects every
    control character wherever it sits, while still accepting a legitimately
    parameterised type (``image/svg+xml; charset=utf-8``). The strip happens
    after, so ``" image/png "`` comes back as ``"image/png"``.

    NOTE for whoever tests a caller of this — the failures are NOT equally
    visible under Starlette's TestClient:

    * the non-ASCII encode happens in ``Response.init_headers``, INSIDE the app,
      so TestClient does surface it (raises ``UnicodeEncodeError``, or 500s with
      ``raise_server_exceptions=False``);
    * the control-character and padding shapes are passed through untouched and
      only die at the HTTP framing layer, so they return a green 200 through
      TestClient and fail only on a real server.

    So a TestClient status assertion is enough for one shape and useless for the
    others. Assert on the content-type VALUE that gets served, and unit-test
    this function directly — passing the raw value, not a pre-stripped one, or
    the test stops asserting about the input it names.
    """
    if not value.isascii() or not value.isprintable():
        return None
    trimmed = value.strip()
    return trimmed or None


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
