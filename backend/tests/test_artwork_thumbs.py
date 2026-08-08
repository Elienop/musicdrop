import io

import pytest
from PIL import Image

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
