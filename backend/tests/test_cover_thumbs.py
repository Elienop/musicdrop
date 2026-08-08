from __future__ import annotations

import io
from pathlib import Path

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
