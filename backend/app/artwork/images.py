# backend/app/artwork/images.py
"""Generic image validation shared by the cover + artist-image endpoints.

No beets here — magic-byte sniffing and the upload size cap are plain image
utilities, so they live OUTSIDE the beets adapter and can be imported by any
router without pulling beets in (CLAUDE.md rule 3).
"""

from __future__ import annotations

from typing import Final

# Hard cap on image bytes any upload endpoint will materialize (DoS guard).
MAX_IMAGE_BYTES: Final = 10 * 1024 * 1024


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
