import pytest

from app.artwork.images import (
    FALLBACK_CONTENT_TYPE,
    MAX_IMAGE_BYTES,
    is_header_safe_content_type,
    sniff_image_mime,
)


def test_sniffs_png() -> None:
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\n....") == "image/png"


def test_sniffs_jpeg() -> None:
    assert sniff_image_mime(b"\xff\xd8\xff....") == "image/jpeg"


def test_sniffs_gif() -> None:
    assert sniff_image_mime(b"GIF89a....") == "image/gif"


def test_sniffs_webp() -> None:
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBP....") == "image/webp"


def test_rejects_non_image() -> None:
    assert sniff_image_mime(b"not an image") is None


def test_cap_is_ten_mib() -> None:
    assert MAX_IMAGE_BYTES == 10 * 1024 * 1024


# Content-type values that must survive untouched. The parameterised ones are the
# reason the predicate is not a naive `in {"image/png", ...}` allowlist.
@pytest.mark.parametrize(
    "value",
    [
        "image/png",
        "image/jpeg",
        "image/webp",
        FALLBACK_CONTENT_TYPE,
        "image/svg+xml; charset=utf-8",
        'image/png; name="a b.png"',
    ],
)
def test_header_safe_accepts_real_content_types(value: str) -> None:
    assert is_header_safe_content_type(value) is True


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("image/\u65e5\u672c\u8a9e", "non-ASCII: Starlette encodes headers latin-1 -> 500"),
        ("image/png\nX-Injected: yes", "response splitting: h11 drops the connection"),
        ("image/png\r\nX-Injected: yes", "response splitting, CRLF form"),
        ("image/png\x00", "NUL is not printable"),
        ("image/png\tx", "tab is not printable"),
        ("", "empty Content-Type makes browsers sniff the body"),
        ("   ", "whitespace-only survives .strip() as empty"),
    ],
)
def test_header_safe_rejects_unsendable_content_types(value: str, why: str) -> None:
    assert is_header_safe_content_type(value.strip()) is False, why
