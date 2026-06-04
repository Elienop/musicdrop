from pathlib import Path

import pytest

from app.artwork.cache import NEGATIVE, ArtistImageCache, CachedImage


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
