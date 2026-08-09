from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import pytest
from PIL import Image

from app.artwork.cover_thumbs import CoverThumbCache


def _png(width: int, height: int, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def test_derives_stores_and_reuses(tmp_path: Path) -> None:
    cache = CoverThumbCache(tmp_path)
    calls: list[int] = []

    def load() -> tuple[bytes, str] | None:
        calls.append(1)
        return (_png(1200, 1200), "image/png")

    first = cache.get(7, '"100-5"', load)
    assert first is not None and first.content_type == "image/webp"
    second = cache.get(7, '"100-5"', load)
    assert second is not None and second.data == first.data
    assert len(calls) == 1  # unchanged source never reloaded


def test_source_change_rederives(tmp_path: Path) -> None:
    cache = CoverThumbCache(tmp_path)
    a = cache.get(7, '"100-5"', lambda: (_png(1200, 1200, "red"), "image/png"))
    b = cache.get(7, '"200-9"', lambda: (_png(1200, 1200, "blue"), "image/png"))
    assert a is not None and b is not None and a.data != b.data


def test_missing_original_returns_none(tmp_path: Path) -> None:
    assert CoverThumbCache(tmp_path).get(7, '"1-1"', lambda: None) is None


def test_undecodable_original_degrades_to_original_bytes(tmp_path: Path) -> None:
    got = CoverThumbCache(tmp_path).get(7, '"1-1"', lambda: (b"garbage", "image/png"))
    assert got is not None and got.data == b"garbage" and got.content_type == "image/png"


def test_degrade_leaves_an_operator_visible_trace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Same promise as the artist cache: a degrade names the album and the
    reason, and does not change what is served. A systemic encoder failure has
    to be findable — silently serving full-size covers forever looks exactly
    like the thumb feature never shipping.
    """
    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        got = CoverThumbCache(tmp_path).get(7, '"1-1"', lambda: (b"garbage", "image/png"))

    assert got is not None and got.data == b"garbage" and got.content_type == "image/png"
    (record,) = [r for r in caplog.records if r.name == "musicdrop.artwork"]
    assert record.levelno == logging.WARNING
    assert "album 7" in record.getMessage()
    assert "UnidentifiedImageError" in record.getMessage()


def test_unsendable_stored_mime_rederives(tmp_path: Path) -> None:
    """A VALID-UTF-8 ``.src`` can still hold a type no response can carry.

    The non-UTF-8 case already self-healed through the decode guard; this one
    decodes fine and went straight into Content-Type, where a non-ASCII value
    500s the cover endpoint (Starlette encodes headers latin-1). Re-deriving
    replaces it with ``image/webp`` rather than a generic fallback.
    """
    cache = CoverThumbCache(tmp_path)
    assert cache.get(7, '"1-1"', lambda: (_png(400, 400), "image/png")) is not None
    (tmp_path / "7.src").write_text('"1-1" image/日本語', encoding="utf-8")

    healed = cache.get(7, '"1-1"', lambda: (_png(400, 400), "image/png"))

    assert healed is not None and healed.content_type == "image/webp"


def test_stored_mime_with_a_control_char_rederives(tmp_path: Path) -> None:
    """``.src`` is not stripped on read, so a trailing newline reaches
    Content-Type verbatim — "Empty reply from server" on a real uvicorn under
    both workers. Re-deriving replaces it with ``image/webp``."""
    cache = CoverThumbCache(tmp_path)
    assert cache.get(7, '"1-1"', lambda: (_png(400, 400), "image/png")) is not None
    (tmp_path / "7.src").write_text('"1-1" image/webp\n', encoding="utf-8")

    healed = cache.get(7, '"1-1"', lambda: (_png(400, 400), "image/png"))

    assert healed is not None and healed.content_type == "image/webp"


def test_padded_stored_mime_is_served_trimmed(tmp_path: Path) -> None:
    """Trimmed, not refused: the type is fine, only the framing is not — and
    refusing would re-derive on every request forever. ``load_original``
    exploding is what proves this took the HIT path."""
    cache = CoverThumbCache(tmp_path)
    assert cache.get(7, '"1-1"', lambda: (_png(400, 400), "image/png")) is not None
    (tmp_path / "7.src").write_text('"1-1"   image/webp  ', encoding="utf-8")

    def _boom() -> tuple[bytes, str]:
        raise AssertionError("a padded mime must hit, not re-derive")

    served = cache.get(7, '"1-1"', _boom)

    assert served is not None and served.content_type == "image/webp"


def test_degrade_sanitises_the_originals_mime(tmp_path: Path) -> None:
    """On the degrade path the ORIGINAL's mime goes on the wire and into
    ``.src``, so it has to clear the same bar a stored one does."""
    got = CoverThumbCache(tmp_path).get(7, '"1-1"', lambda: (b"garbage", "image/日本語"))

    assert got is not None and got.data == b"garbage"
    assert got.content_type == "application/octet-stream"
    assert (tmp_path / "7.src").read_text(encoding="utf-8") == '"1-1" application/octet-stream'


def test_read_only_cache_dir_still_serves_the_thumb(tmp_path: Path) -> None:
    """The thumb is derived before it is written back, so an unwritable cache
    dir must cost the caching, not the cover."""
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    cache_dir = tmp_path / "covers"
    cache_dir.mkdir()
    os.chmod(cache_dir, 0o500)
    try:
        got = CoverThumbCache(cache_dir).get(7, '"1-1"', lambda: (_png(400, 400), "image/png"))
    finally:
        os.chmod(cache_dir, 0o755)

    assert got is not None and got.content_type == "image/webp"
    assert not list(cache_dir.glob("*.bin"))  # nothing was cached


def test_corrupt_src_sidecar_self_heals(tmp_path: Path) -> None:
    """A non-UTF-8 ``.src`` re-derives instead of 500ing the cover endpoint.

    ``read_text`` raises UnicodeDecodeError, which ``except OSError`` does not
    catch — and this cache is documented as safe to delete entirely, so any
    unreadable sidecar must simply rebuild.
    """
    cache = CoverThumbCache(tmp_path)
    assert cache.get(7, '"1-1"', lambda: (_png(400, 400), "image/png")) is not None
    (tmp_path / "7.src").write_bytes(b"\xff\xfe not utf-8")
    healed = cache.get(7, '"1-1"', lambda: (_png(400, 400), "image/png"))
    assert healed is not None and healed.content_type == "image/webp"
