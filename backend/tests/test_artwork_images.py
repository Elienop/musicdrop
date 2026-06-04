from app.artwork.images import MAX_IMAGE_BYTES, sniff_image_mime


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
