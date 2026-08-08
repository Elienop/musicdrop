import pytest

from app.artwork.images import (
    FALLBACK_CONTENT_TYPE,
    MAX_IMAGE_BYTES,
    header_safe_content_type,
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


# Content-type values that must survive UNTOUCHED. The parameterised ones are
# the reason this is not a naive `in {"image/png", ...}` allowlist.
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
def test_header_safe_returns_a_real_content_type_unchanged(value: str) -> None:
    assert header_safe_content_type(value) == value


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("image/日本語", "non-ASCII: Starlette encodes headers latin-1 -> 500"),
        ("image/png\nX-Injected: yes", "response splitting: h11 drops the connection"),
        ("image/png\r\nX-Injected: yes", "response splitting, CRLF form"),
        ("image/png\x00", "NUL is not printable"),
        ("image/png\tx", "an embedded tab is not printable"),
        ("image/png\n", "TRAILING newline: 'Empty reply' under h11 AND httptools"),
        ("image/png\r", "trailing CR: 'Empty reply' under h11 AND httptools"),
        ("image/png\t", "trailing tab: h11 fullmatch rejects it"),
        ("\timage/png", "leading tab: h11 fullmatch rejects it"),
        ("", "empty Content-Type makes browsers sniff the body"),
        ("   ", "whitespace-only: isascii() and isprintable() are both True for it"),
        ("\n", "a bare newline is not printable"),
    ],
)
def test_header_safe_refuses_a_value_that_cannot_be_sent(value: str, why: str) -> None:
    # Pass the value RAW. An earlier version of this test called
    # `header_safe_content_type(value.strip())`, so the "   " case actually
    # asserted about "" — production accepted whitespace-only and the test went
    # green anyway. A transformation between a parameter and its assertion is
    # invisible to mutation testing, which only asks whether SOME test fails.
    assert header_safe_content_type(value) is None, why


def test_header_safe_trims_padding_rather_than_refusing_it() -> None:
    """Padding is a FRAMING problem, not a bad type — so trim, don't discard.

    ``" image/png "`` is a dropped connection under uvicorn's h11 worker (h11
    fullmatches ``([^\\x00\\s]+(?:[ \\t]+[^\\x00\\s]+)*)?``, so any leading or
    trailing whitespace is an illegal field value); httptools tolerates it. Both
    were measured. An earlier version of this file asserted the padded value was
    ACCEPTABLE and justified it as protecting a sloppy CDN's header — which is
    doubly wrong: h11 drops it, and httpx strips OWS on parse so a CDN cannot
    deliver it in the first place.

    Returning the trimmed VALUE is what makes this safe at every caller. A bool
    that trimmed internally would have widened acceptance while callers kept
    sending the original, and no mutation of this function's own line could see
    that.
    """
    assert header_safe_content_type("  image/png  ") == "image/png"
