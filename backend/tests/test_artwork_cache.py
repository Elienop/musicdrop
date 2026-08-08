from __future__ import annotations

import contextlib
import io
import logging
import os
import unittest.mock
from pathlib import Path

import pytest
from PIL import Image

from app.artwork.cache import NEGATIVE, ArtistImageCache, CachedImage

_StrPath = str | os.PathLike[str]


def _png(width: int, height: int, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def cache(tmp_path: Path) -> ArtistImageCache:
    return ArtistImageCache(tmp_path)


def test_get_miss_when_empty(cache: ArtistImageCache) -> None:
    assert cache.get("ABBA") is None


def test_store_and_get_positive_roundtrips_bytes_and_mime(cache: ArtistImageCache) -> None:
    cache.store_positive("ABBA", b"\x89PNGdata", "image/png")
    result = cache.get("ABBA")
    assert isinstance(result, CachedImage)
    assert result.data == b"\x89PNGdata"
    assert result.content_type == "image/png"


def test_positive_lookup_is_normalized(cache: ArtistImageCache) -> None:
    # Stored under one casing, fetched under another -> same slot.
    cache.store_positive("Beyoncé", b"img", "image/jpeg")
    result = cache.get("beyonce")
    assert isinstance(result, CachedImage)
    assert result.data == b"img"


def test_store_negative_then_get_returns_negative(cache: ArtistImageCache) -> None:
    cache.store_negative("Nobody", ttl_seconds=3600)
    assert cache.get("Nobody") is NEGATIVE


def test_negative_expires_after_ttl(cache: ArtistImageCache) -> None:
    cache.store_negative("Nobody", ttl_seconds=0)
    # TTL of 0 -> expiry is now -> immediately stale -> treated as a fresh miss.
    assert cache.get("Nobody") is None


def test_negative_marker_stores_explicit_expiry(cache: ArtistImageCache, tmp_path: Path) -> None:
    import time

    cache.store_negative("Nobody", ttl_seconds=600)
    key = cache._key("Nobody")
    expiry = float((tmp_path / f"{key}.miss").read_text(encoding="utf-8").strip())
    # The marker stores now+ttl, not just now.
    assert expiry == pytest.approx(time.time() + 600, abs=5)


def test_short_ttl_goes_stale_before_long_ttl(cache: ArtistImageCache) -> None:
    # A transient (short) marker expires sooner than a confirmed (long) one.
    cache.store_negative("Transient", ttl_seconds=0)
    cache.store_negative("Confirmed", ttl_seconds=3600)
    assert cache.get("Transient") is None
    assert cache.get("Confirmed") is NEGATIVE


def test_unparseable_marker_treated_stale(cache: ArtistImageCache, tmp_path: Path) -> None:
    cache.store_negative("Nobody", ttl_seconds=3600)
    key = cache._key("Nobody")
    (tmp_path / f"{key}.miss").write_text("garbage", encoding="utf-8")
    # Robust fallback: a corrupt expiry is treated as stale (re-resolve).
    assert cache.get("Nobody") is None


def test_override_wins_over_positive_and_negative(cache: ArtistImageCache) -> None:
    cache.store_positive("ABBA", b"auto", "image/png")
    cache.store_negative("ABBA", ttl_seconds=3600)
    # Manually plant an override slot (no writer in this chunk).
    cache.write_override("ABBA", b"manual", "image/jpeg")
    result = cache.get("ABBA")
    assert isinstance(result, CachedImage)
    assert result.data == b"manual"
    assert result.content_type == "image/jpeg"


def test_override_wins_even_with_fresh_negative(cache: ArtistImageCache) -> None:
    cache.write_override("ABBA", b"manual", "image/png")
    cache.store_negative("ABBA", ttl_seconds=3600)
    result = cache.get("ABBA")
    assert isinstance(result, CachedImage)
    assert result.data == b"manual"


def test_positive_mime_written_before_bytes(cache: ArtistImageCache, tmp_path: Path) -> None:
    # Hard to observe ordering directly; assert both land and roundtrip, and
    # that a get never sees bytes without a mime (covered by other tests).
    cache.store_positive("ABBA", b"data", "image/png")
    key = cache._key("ABBA")
    assert (tmp_path / f"{key}.mime").exists()
    assert (tmp_path / f"{key}.bin").exists()


def test_clear_override_removes_both_files_and_falls_through(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    cache.store_positive("ABBA", b"auto", "image/png")
    cache.write_override("ABBA", b"manual", "image/jpeg")
    cache.clear_override("ABBA")
    key = cache._key("ABBA")
    assert not (tmp_path / f"{key}.override").exists()
    assert not (tmp_path / f"{key}.override.mime").exists()
    # With the override gone, get() falls through to the positive slot.
    result = cache.get("ABBA")
    assert isinstance(result, CachedImage)
    assert result.data == b"auto"


def test_clear_override_is_idempotent_when_absent(cache: ArtistImageCache) -> None:
    cache.clear_override("Nobody")  # no error, no-op
    assert cache.get("Nobody") is None


def test_store_positive_publishes_bin_via_atomic_replace(
    cache: ArtistImageCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image bytes must be published by an atomic rename, never a truncate-
    then-write in place. The artist-art backfill daemon writes this cache on its
    own thread while the event loop serves GET /api/artists/image over the same
    files; an in-place write lets a reader catch a half-written (truncated)
    image and serve it with a 200 + content-hash ETag."""
    key = cache._key("ABBA")
    bin_path = tmp_path / f"{key}.bin"
    cache.store_positive("ABBA", b"GOODCOMPLETE", "image/png")  # seed a full image

    replaced: list[str] = []
    real_replace = os.replace

    def spy(src: _StrPath, dst: _StrPath) -> None:
        replaced.append(os.fspath(dst))
        real_replace(src, dst)  # pass-through spy

    monkeypatch.setattr(os, "replace", spy)
    cache.store_positive("ABBA", b"NEWCOMPLETE", "image/png")

    assert str(bin_path) in replaced  # .bin came from os.replace, not in-place write
    result = cache.get("ABBA")
    assert isinstance(result, CachedImage)
    assert result.data == b"NEWCOMPLETE"


def test_store_positive_crash_before_publish_keeps_last_good_image(
    cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must not cache a corrupt image forever: the target is
    only ever published atomically, so an interrupted overwrite leaves the last
    good image intact (get() has no TTL/validation on the positive slot, so a
    truncated .bin would otherwise be served indefinitely).

    The crash is injected on the ``.bin`` publish specifically (letting the .mime
    sidecar succeed first) so the positive-slot image-bytes path — the one the
    docstring protects — is the path actually interrupted."""
    cache.store_positive("ABBA", b"GOODCOMPLETE", "image/png")  # last-good

    real_replace = os.replace

    def boom_on_bin(src: _StrPath, dst: _StrPath) -> None:
        if os.fspath(dst).endswith(".bin"):
            raise OSError("simulated crash publishing the .bin")
        real_replace(src, dst)  # let the .mime sidecar land

    monkeypatch.setattr(os, "replace", boom_on_bin)
    with contextlib.suppress(OSError):
        cache.store_positive("ABBA", b"TRUNCATED", "image/png")  # crashes on .bin publish

    result = cache.get("ABBA")
    assert isinstance(result, CachedImage)
    assert result.data == b"GOODCOMPLETE"  # the .bin overwrite never tore the target


def test_write_override_publishes_via_atomic_replace(
    cache: ArtistImageCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override upload racing an image GET has the same torn-read window —
    it too must publish via an atomic rename."""
    key = cache._key("ABBA")
    override_path = tmp_path / f"{key}.override"
    cache.write_override("ABBA", b"FIRST", "image/png")

    replaced: list[str] = []
    real_replace = os.replace

    def spy(src: _StrPath, dst: _StrPath) -> None:
        replaced.append(os.fspath(dst))
        real_replace(src, dst)  # pass-through spy

    monkeypatch.setattr(os, "replace", spy)
    cache.write_override("ABBA", b"SECOND", "image/jpeg")

    assert str(override_path) in replaced
    result = cache.get("ABBA")
    assert isinstance(result, CachedImage)
    assert result.data == b"SECOND"
    assert result.content_type == "image/jpeg"


def test_validator_none_when_uncached(cache: ArtistImageCache) -> None:
    assert cache.validator("ABBA") is None


def test_validator_tracks_positive_slot(cache: ArtistImageCache) -> None:
    cache.store_positive("ABBA", b"png-bytes", "image/png")
    tag = cache.validator("ABBA")
    assert tag is not None and tag.startswith('"') and tag.endswith('"')
    # Unchanged file -> same tag; rewritten file -> different tag.
    assert cache.validator("ABBA") == tag
    cache.store_positive("ABBA", b"other-bytes-longer", "image/png")
    assert cache.validator("ABBA") != tag


def test_validator_prefers_override(cache: ArtistImageCache) -> None:
    cache.store_positive("ABBA", b"auto", "image/png")
    auto_tag = cache.validator("ABBA")
    cache.write_override("ABBA", b"manual-bytes", "image/jpeg")
    override_tag = cache.validator("ABBA")
    assert override_tag != auto_tag
    cache.clear_override("ABBA")
    assert cache.validator("ABBA") == auto_tag


def test_get_thumb_derives_and_reuses(cache: ArtistImageCache) -> None:
    cache.store_positive("ABBA", _png(1000, 1000), "image/png")
    thumb = cache.get_thumb("ABBA")
    assert thumb is not None and thumb.content_type == "image/webp"
    assert Image.open(io.BytesIO(thumb.data)).size == (320, 320)
    # Second call serves the stored derivation (no re-encode): patching
    # make_thumb to explode proves it isn't called again.
    with unittest.mock.patch("app.artwork.cache.make_thumb", side_effect=AssertionError):
        again = cache.get_thumb("ABBA")
    assert again is not None and again.data == thumb.data


def test_get_thumb_regenerates_when_source_changes(cache: ArtistImageCache) -> None:
    cache.store_positive("ABBA", _png(1000, 1000, "red"), "image/png")
    first = cache.get_thumb("ABBA")
    cache.write_override("ABBA", _png(900, 900, "blue"), "image/png")
    second = cache.get_thumb("ABBA")
    assert second is not None and first is not None and second.data != first.data


def test_get_thumb_none_when_uncached(cache: ArtistImageCache) -> None:
    assert cache.get_thumb("Nobody") is None


def test_get_thumb_falls_back_to_original_on_undecodable_source(
    cache: ArtistImageCache,
) -> None:
    cache.store_positive("ABBA", b"corrupt-not-an-image", "image/png")
    thumb = cache.get_thumb("ABBA")
    # Serve-or-degrade: a source Pillow can't read serves the original bytes.
    assert thumb is not None and thumb.data == b"corrupt-not-an-image"
    assert thumb.content_type == "image/png"
    # The degrade result is cached too (keyed to the source tag) — a second
    # call must not attempt make_thumb again either.
    with unittest.mock.patch("app.artwork.cache.make_thumb", side_effect=AssertionError):
        again = cache.get_thumb("ABBA")
    assert again is not None and again.data == b"corrupt-not-an-image"
    assert again.content_type == "image/png"


def test_get_thumb_degrade_leaves_an_operator_visible_trace(
    cache: ArtistImageCache, caplog: pytest.LogCaptureFixture
) -> None:
    """A degrade is correct but must not be silent: if Pillow ever loses WebP,
    every image degrades to full-size forever and the symptom is
    indistinguishable from the thumb feature never having been deployed.

    The record has to name WHICH entity degraded and WHY (ThumbError carries the
    underlying exception's type name), and must not change what is served.
    """
    cache.store_positive("ABBA", b"corrupt-not-an-image", "image/png")
    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        thumb = cache.get_thumb("ABBA")

    assert thumb is not None
    assert thumb.data == b"corrupt-not-an-image"
    assert thumb.content_type == "image/png"
    (record,) = [r for r in caplog.records if r.name == "musicdrop.artwork"]
    assert record.levelno == logging.WARNING
    assert "ABBA" in record.getMessage()
    assert "UnidentifiedImageError" in record.getMessage()


def test_get_thumb_degrade_with_blank_mime_still_caches(cache: ArtistImageCache) -> None:
    """A blank source mime must not disable the thumb cache forever.

    A whitespace-only ``.mime`` sidecar reads back stripped to ``""``. Written
    bare into ``.thumb.src`` that leaves a trailing space, so the next read's
    ``stored_mime`` is falsy and the entry never hits — the thumb re-derives on
    EVERY request. CoverThumbCache already falls back to
    ``application/octet-stream`` here; the artist side must match.
    """
    cache.store_positive("ABBA", b"corrupt-not-an-image", "   ")
    thumb = cache.get_thumb("ABBA")
    assert thumb is not None and thumb.content_type == "application/octet-stream"
    with unittest.mock.patch("app.artwork.cache.make_thumb", side_effect=AssertionError):
        again = cache.get_thumb("ABBA")
    assert again is not None and again.data == b"corrupt-not-an-image"


def test_get_thumb_self_heals_from_corrupt_src_sidecar(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """A non-UTF-8 ``.thumb.src`` re-derives instead of 500ing the endpoint.

    ``read_text`` raises UnicodeDecodeError, which ``except OSError`` does not
    catch — and the image endpoint has no guard of its own.
    """
    cache.store_positive("ABBA", _png(400, 400), "image/png")
    assert cache.get_thumb("ABBA") is not None
    (src_path,) = tmp_path.glob("*.thumb.src")
    src_path.write_bytes(b"\xff\xfe not utf-8")
    healed = cache.get_thumb("ABBA")
    assert healed is not None and healed.content_type == "image/webp"
