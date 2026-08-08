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

from PIL import Image, ImageOps

THUMB_MAX_DIM: Final[int] = 320
THUMB_MIME: Final[str] = "image/webp"
_WEBP_QUALITY: Final[int] = 80


class ThumbError(ValueError):
    """No thumbnail could be derived from the input bytes."""


def make_thumb(data: bytes, *, max_dim: int = THUMB_MAX_DIM) -> bytes:
    """Derive a WebP thumbnail from arbitrary image bytes.

    Either returns usable bytes or raises :class:`ThumbError` — no other
    exception type escapes, for any input. Both caches promise serve-or-degrade
    and never a 500, and they implement that by catching ThumbError; anything
    else escaping breaks the promise at exactly the moment it matters, on
    hostile or malformed art.

    That is why the guard is a blanket ``except Exception`` rather than an
    enumerated tuple. Pillow's failure surface is not reachable from one base
    class — ``DecompressionBombError`` derives straight from ``Exception``, and
    each format plugin adds its own — and every step below (transpose, mode
    convert, resample, encode) runs on the same untrusted pixels the decode
    does, so all of it belongs inside the guard. There is no failure here a
    caller wants to treat as anything other than "no thumb for these bytes".
    """
    try:
        img_file = Image.open(io.BytesIO(data))
        img_file.load()
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
    except Exception as exc:
        # Type name included: some Pillow exceptions stringify to "".
        raise ThumbError(f"{type(exc).__name__}: {exc}") from exc
