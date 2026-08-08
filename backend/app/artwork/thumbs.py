"""Derived-thumbnail engine: full-size art in, small WebP out.

Grid cards render at 128-160 CSS px; the sources are 1000px+ originals
(fanart.tv / Deezer XL / album art files), commonly 100 KB-1 MB each. A 320px
WebP (~10-40 KB) covers 2x-DPI cards and the 160px detail headers. Pure
function of the input bytes - callers cache the output keyed by a validator of
the SOURCE file (see ArtistImageCache.get_thumb / CoverThumbCache), so a thumb
never needs regenerating while its source is unchanged.
"""

from __future__ import annotations

import io
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

THUMB_MAX_DIM: Final[int] = 320
THUMB_MIME: Final[str] = "image/webp"
_WEBP_QUALITY: Final[int] = 80


class ThumbError(ValueError):
    """The input bytes could not be decoded as an image."""


def make_thumb(data: bytes, *, max_dim: int = THUMB_MAX_DIM) -> bytes:
    try:
        img_file = Image.open(io.BytesIO(data))
        img_file.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ThumbError(str(exc)) from exc
    # Camera-orientation flag → real pixels (a thumb has no EXIF to carry it).
    img: Image.Image = ImageOps.exif_transpose(img_file)
    # Animated sources contribute their first frame; palette/CMYK/1-bit modes
    # normalize so WebP encode can't reject them. Alpha is preserved.
    if img.mode not in {"RGB", "RGBA"}:
        img = img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
    img.thumbnail((max_dim, max_dim))  # in-place, never upscales
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=_WEBP_QUALITY)
    return out.getvalue()
