import io

import pytest
from PIL import Image, ImageOps

from app.artwork.thumbs import THUMB_MAX_DIM, ThumbError, make_thumb


def _png(width: int, height: int, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def test_downscales_to_max_dim_preserving_aspect() -> None:
    out = make_thumb(_png(1000, 500))
    img = Image.open(io.BytesIO(out))
    assert img.format == "WEBP"
    assert img.size == (THUMB_MAX_DIM, THUMB_MAX_DIM // 2)


def test_small_image_is_not_upscaled() -> None:
    out = make_thumb(_png(100, 80))
    assert Image.open(io.BytesIO(out)).size == (100, 80)


def test_alpha_survives() -> None:
    buf = io.BytesIO()
    Image.new("RGBA", (800, 800), (255, 0, 0, 128)).save(buf, format="PNG")
    img = Image.open(io.BytesIO(make_thumb(buf.getvalue())))
    assert img.mode in {"RGBA", "LA"}


def test_undecodable_raises_thumberror() -> None:
    with pytest.raises(ThumbError):
        make_thumb(b"not an image at all")


def test_decompression_bomb_raises_thumberror(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pixel bomb degrades like any other bad input.

    Pillow's DecompressionBombError derives straight from Exception (NOT from
    OSError/ValueError), so an enumerated except-tuple lets it escape past the
    callers' degrade path and 500 the image endpoint. Lowering MAX_IMAGE_PIXELS
    is how a bomb is reproduced without allocating one.
    """
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    png = _png(100, 100)
    with pytest.raises(ThumbError):
        make_thumb(png)


def test_no_exception_type_other_than_thumberror_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole pipeline — not just decode — is inside the guard.

    exif_transpose/convert/thumbnail/save run on attacker-supplied pixels too,
    so anything they raise must arrive at the caller as ThumbError; the caches
    promise serve-or-degrade, never a 500.
    """

    def _boom(_image: Image.Image) -> Image.Image:
        raise RuntimeError("pipeline blew up")

    # thumbs.py holds the same PIL.ImageOps module object and resolves the
    # attribute at call time, so patching it here reaches the call there.
    monkeypatch.setattr(ImageOps, "exif_transpose", _boom)
    pixels = _png(400, 400)
    with pytest.raises(ThumbError):
        make_thumb(pixels)


def test_thumb_is_smaller_than_a_real_photo_original() -> None:
    # Noise compresses badly — a solid color would trivially pass. This guards
    # the point of the feature: grid bytes shrink by an order of magnitude.
    import random

    rng = random.Random(42)
    img = Image.new("RGB", (1000, 1000))
    pixels = [
        (rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(1000 * 1000)
    ]
    img.putdata(pixels)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    original = buf.getvalue()
    assert len(make_thumb(original)) < len(original) / 4
