from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import threading
import time
import unittest.mock
from pathlib import Path

import pytest
from PIL import Image

from app.artwork.cache import (
    _ALL_SLOT_SUFFIXES,
    _MOVE_ORDER,
    NEGATIVE,
    ArtistImageCache,
    CachedImage,
    _is_memory_tag,
    _MemoryEntry,
    _NegativeUntil,
)
from app.etag import stat_etag

_StrPath = str | os.PathLike[str]


def _png(width: int, height: int, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def cache(tmp_path: Path) -> ArtistImageCache:
    return ArtistImageCache(tmp_path)


def _broken_cache_dir(tmp_path: Path) -> Path:
    """A cache dir that genuinely cannot be created, read or written — as root too.

    The path's parent is a regular FILE, so ``mkdir`` raises ENOTDIR and every
    slot ``exists()`` reads False (pathlib swallows ENOTDIR). That matters
    because the ``chmod 0o500`` fixtures this file already uses have to ``skip``
    under root, and root is a real way to run this suite: the shipped image
    declares no ``USER`` (``Dockerfile``), so a maintainer running pytest inside
    it is root and a write-back test built on chmod would report green having
    executed nothing. (Not CI: ``.github/workflows/ci.yml`` runs the job on
    ``ubuntu-latest`` with no ``container:`` key and no ``sudo``, i.e. as
    ``runner``.)
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_bytes(b"")
    return blocker / "cache"


def _strand(cache: ArtistImageCache, name: str, data: bytes, mime: str) -> _MemoryEntry:
    """Put ``name`` in the memory tier the way a failed write would, and hand
    back the entry.

    Inserted directly rather than staged with ``chmod``: that is the root-safe
    pattern the rest of this file already uses for fallback fixtures.
    """
    cache._memory.put(cache._key(name), CachedImage(data=data, content_type=mime))
    entry = cache._memory.get(cache._key(name))
    assert isinstance(entry, _MemoryEntry)
    return entry


def _artwork_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Records from THIS package's logger only.

    Asserting on ``caplog.records`` wholesale makes a test hostage to any
    unrelated WARNING any other logger happens to emit during it.
    """
    return [record for record in caplog.records if record.name == "musicdrop.artwork"]


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
    assert tag is not None
    assert tag.startswith('"')
    assert tag.endswith('"')
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


def test_get_survives_a_non_utf8_mime_sidecar(
    cache: ArtistImageCache, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt sidecar must not cost the image.

    ``read_text`` raises UnicodeDecodeError — a ValueError, so ``except
    OSError`` would not have caught it — and neither image endpoint guards its
    own call, so this was a 500 on the wire. The image itself is still perfectly
    good; it just loses its declared type.
    """
    cache.store_positive("ABBA", b"image-bytes", "image/png")
    (mime_path,) = tmp_path.glob("*.mime")
    mime_path.write_bytes(b"\xff\xfe")

    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        got = cache.get("ABBA")

    assert isinstance(got, CachedImage)
    assert got.data == b"image-bytes"
    assert got.content_type == "application/octet-stream"
    assert len(_artwork_records(caplog)) == 1


def test_get_treats_unreadable_bytes_as_nothing_cached(
    cache: ArtistImageCache, caplog: pytest.LogCaptureFixture
) -> None:
    """``exists()`` then ``read_bytes()`` is a window the backfill daemon's
    atomic replace can move under us. Answering None (rather than raising) sends
    the caller down its normal resolve path, so the entry self-heals.
    """
    cache.store_positive("ABBA", b"image-bytes", "image/png")

    with (
        unittest.mock.patch.object(Path, "read_bytes", side_effect=OSError(5, "I/O error")),
        caplog.at_level(logging.WARNING, logger="musicdrop.artwork"),
    ):
        got = cache.get("ABBA")

    assert got is None
    assert len(_artwork_records(caplog)) == 1


def test_get_thumb_derives_and_reuses(cache: ArtistImageCache) -> None:
    cache.store_positive("ABBA", _png(1000, 1000), "image/png")
    thumb = cache.get_thumb("ABBA")
    assert thumb is not None
    assert thumb.content_type == "image/webp"
    assert Image.open(io.BytesIO(thumb.data)).size == (320, 320)
    # Second call serves the stored derivation (no re-encode): patching
    # make_thumb to explode proves it isn't called again.
    with unittest.mock.patch("app.artwork.degrade.make_thumb", side_effect=AssertionError):
        again = cache.get_thumb("ABBA")
    assert again is not None
    assert again.data == thumb.data


def test_get_thumb_regenerates_when_source_changes(cache: ArtistImageCache) -> None:
    cache.store_positive("ABBA", _png(1000, 1000, "red"), "image/png")
    first = cache.get_thumb("ABBA")
    cache.write_override("ABBA", _png(900, 900, "blue"), "image/png")
    second = cache.get_thumb("ABBA")
    assert second is not None
    assert first is not None
    assert second.data != first.data


def test_get_thumb_none_when_uncached(cache: ArtistImageCache) -> None:
    assert cache.get_thumb("Nobody") is None


def test_get_thumb_falls_back_to_original_on_undecodable_source(
    cache: ArtistImageCache,
) -> None:
    cache.store_positive("ABBA", b"corrupt-not-an-image", "image/png")
    thumb = cache.get_thumb("ABBA")
    # Serve-or-degrade: a source Pillow can't read serves the original bytes.
    assert thumb is not None
    assert thumb.data == b"corrupt-not-an-image"
    assert thumb.content_type == "image/png"
    # The degrade result is cached too (keyed to the source tag) — a second
    # call must not attempt make_thumb again either.
    with unittest.mock.patch("app.artwork.degrade.make_thumb", side_effect=AssertionError):
        again = cache.get_thumb("ABBA")
    assert again is not None
    assert again.data == b"corrupt-not-an-image"
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


def test_get_thumb_degrade_with_blank_mime_still_caches(
    cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank source mime must not disable the thumb cache forever.

    Blank written bare into ``.thumb.src`` leaves a trailing space, so the next
    read's ``stored_mime`` is unusable and the entry never hits — the thumb
    re-derives on EVERY request.

    The blank ``CachedImage`` is CONSTRUCTED here rather than reached through a
    whitespace-only ``.mime`` sidecar, because ``_read_image`` now returns the
    fallback for that and this test would silently stop exercising anything.
    ``get_thumb`` does not own ``_read_image``'s invariant, so its own
    ``or FALLBACK_CONTENT_TYPE`` stays as a second line of defence and this
    seeds the state that reaches it.
    """
    cache.store_positive("ABBA", b"corrupt-not-an-image", "image/png")

    def _blank_typed_source(self: ArtistImageCache, name: str) -> CachedImage:
        return CachedImage(data=b"corrupt-not-an-image", content_type="")

    monkeypatch.setattr(ArtistImageCache, "get", _blank_typed_source)

    thumb = cache.get_thumb("ABBA")
    assert thumb is not None
    assert thumb.content_type == "application/octet-stream"
    with unittest.mock.patch("app.artwork.degrade.make_thumb", side_effect=AssertionError):
        again = cache.get_thumb("ABBA")
    assert again is not None
    assert again.data == b"corrupt-not-an-image"


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
    assert healed is not None
    assert healed.content_type == "image/webp"


@pytest.mark.parametrize(
    "poison",
    ["image/日本語", "image/png\nX-Injected: yes"],
    ids=["non-ascii", "response-splitting"],
)
def test_get_serves_the_fallback_for_an_unsendable_stored_mime(
    cache: ArtistImageCache, tmp_path: Path, poison: str
) -> None:
    """A sidecar that DECODES cleanly can still hold something no HTTP response
    can carry, and that value went straight into Content-Type.

    Both shapes reach disk without corrupting anything: ``download.py`` accepted
    the CDN's header after only ``startswith("image/")``, and httpx decodes
    header bytes as UTF-8. Verified against a real uvicorn — non-ASCII 500s at
    ``Response.init_headers`` (latin-1), and the newline form makes h11 drop the
    connection with no response at all.
    """
    cache.store_positive("ABBA", b"image-bytes", "image/png")
    (mime_path,) = tmp_path.glob("*.mime")
    mime_path.write_text(poison, encoding="utf-8")

    got = cache.get("ABBA")

    assert isinstance(got, CachedImage)
    assert got.data == b"image-bytes"  # the image survives; only its label changes
    assert got.content_type == "application/octet-stream"


def test_get_serves_the_fallback_for_a_blank_stored_mime(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """A readable but EMPTY sidecar must answer like an unreadable one.

    An empty Content-Type is legal on the wire, so nothing 500s — the browser
    just sniffs the body instead, which is exactly what a declared type exists
    to prevent.
    """
    cache.store_positive("ABBA", b"image-bytes", "image/png")
    (mime_path,) = tmp_path.glob("*.mime")
    mime_path.write_bytes(b"")

    got = cache.get("ABBA")

    assert isinstance(got, CachedImage)
    assert got.content_type == "application/octet-stream"


def test_get_thumb_rederives_from_an_unsendable_stored_thumb_mime(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """``.thumb.src`` is the OTHER way a poisoned type reaches the wire.

    A re-derive is the right answer rather than the generic fallback: it
    replaces the label with this cache's own ``image/webp``, so the entry
    genuinely heals instead of serving a WebP as octet-stream forever.
    """
    cache.store_positive("ABBA", _png(400, 400), "image/png")
    assert cache.get_thumb("ABBA") is not None
    (src_path,) = tmp_path.glob("*.thumb.src")
    stored_tag, _, _ = src_path.read_text(encoding="utf-8").partition(" ")
    src_path.write_text(f"{stored_tag} image/日本語", encoding="utf-8")

    healed = cache.get_thumb("ABBA")

    assert healed is not None
    assert healed.content_type == "image/webp"


@pytest.mark.parametrize(
    "poison",
    ["image/webp\n", "image/webp\r", "image/webp\t", "\timage/webp"],
    ids=["trailing-LF", "trailing-CR", "trailing-TAB", "leading-TAB"],
)
def test_get_thumb_rederives_when_the_stored_mime_carries_a_control_char(
    cache: ArtistImageCache, tmp_path: Path, poison: str
) -> None:
    """``.thumb.src`` is the one stored value that is NOT stripped on read —
    ``partition(" ")`` hands back everything after the tag verbatim — so a
    trailing newline from a backup/restore or an operator edit rides straight
    into Content-Type.

    Measured on a real uvicorn, ``image/png\\n`` is "Empty reply from server"
    under BOTH the h11 and httptools workers; h11 also rejects a trailing tab.
    A re-derive is the right answer: it replaces the label with this cache's own
    ``image/webp``.
    """
    cache.store_positive("ABBA", _png(400, 400), "image/png")
    assert cache.get_thumb("ABBA") is not None
    (src_path,) = tmp_path.glob("*.thumb.src")
    stored_tag, _, _ = src_path.read_text(encoding="utf-8").partition(" ")
    src_path.write_text(f"{stored_tag} {poison}", encoding="utf-8")

    healed = cache.get_thumb("ABBA")

    assert healed is not None
    assert healed.content_type == "image/webp"


def test_get_thumb_serves_a_padded_stored_mime_trimmed_without_rederiving(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """Padding is a framing problem, not a bad type: the entry still HITS, and
    what it serves is trimmed.

    Refusing it instead would re-derive on every request forever; serving it raw
    is a dropped connection under the h11 worker. Patching ``make_thumb`` to
    explode is what proves this took the hit path rather than quietly re-deriving.
    """
    cache.store_positive("ABBA", _png(400, 400), "image/png")
    assert cache.get_thumb("ABBA") is not None
    (src_path,) = tmp_path.glob("*.thumb.src")
    stored_tag, _, _ = src_path.read_text(encoding="utf-8").partition(" ")
    src_path.write_text(f"{stored_tag}   image/webp  ", encoding="utf-8")

    with unittest.mock.patch("app.artwork.degrade.make_thumb", side_effect=AssertionError):
        served = cache.get_thumb("ABBA")

    assert served is not None
    assert served.content_type == "image/webp"


def test_a_dropped_negative_marker_still_bounds_refetches(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """The expensive half of a broken cache dir is the marker it cannot write.

    ``.miss`` is the ONLY brake on re-fetching a confirmed no-match. Swallowing
    a failed write turns a 7-day TTL into an upstream call on every request,
    forever and invisibly — and one no-match resolve walks fanart.tv -> Spotify
    -> Deezer, so a 48-card roster paint becomes up to 144 third-party calls
    against a 5/s limiter. The in-memory stand-in is what keeps the TTL a TTL.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    os.chmod(tmp_path, 0o500)
    try:
        cache.store_negative("Nobody", ttl_seconds=3600)
        assert cache.get("Nobody") is NEGATIVE  # would be None -> re-resolve
    finally:
        os.chmod(tmp_path, 0o755)
    assert not list(tmp_path.glob("*.miss"))  # nothing reached disk


def test_an_in_memory_negative_still_expires(cache: ArtistImageCache, tmp_path: Path) -> None:
    """The stand-in carries the TTL, not just the fact — an expired one must let
    the caller re-resolve exactly as an expired ``.miss`` body does."""
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    os.chmod(tmp_path, 0o500)
    try:
        cache.store_negative("Nobody", ttl_seconds=-1)
        assert cache.get("Nobody") is None
    finally:
        os.chmod(tmp_path, 0o755)


def test_a_dropped_positive_is_served_from_memory(cache: ArtistImageCache, tmp_path: Path) -> None:
    """A found artist re-resolves per request too, so the stand-in holds
    positives as well — that is the difference between "degraded to a smaller
    cache" and "no cache at all"."""
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    os.chmod(tmp_path, 0o500)
    try:
        cache.store_positive("ABBA", b"image-bytes", "image/png")
        got = cache.get("ABBA")
    finally:
        os.chmod(tmp_path, 0o755)

    assert isinstance(got, CachedImage)
    assert got.data == b"image-bytes"
    assert got.content_type == "image/png"


def test_a_recovered_cache_dir_takes_authority_back_from_memory(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """Fixing the permissions must be enough — no restart — and the stand-in has
    to be DROPPED, not merely out-voted.

    Reading disk first already means a live disk entry wins, so that alone
    proves nothing about the discard. The seam is what happens once the disk
    entry goes away again: without the drop, ``get`` falls through to a
    months-old in-memory copy and resurrects an image the cache no longer has.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    os.chmod(tmp_path, 0o500)
    try:
        cache.store_positive("ABBA", b"from-memory", "image/png")
    finally:
        os.chmod(tmp_path, 0o755)

    cache.store_positive("ABBA", b"from-disk", "image/png")
    got = cache.get("ABBA")
    assert isinstance(got, CachedImage)
    assert got.data == b"from-disk"

    for stored in tmp_path.iterdir():
        stored.unlink()
    assert cache.get("ABBA") is None, "a stale stand-in must not outlive its disk entry"


def test_the_memory_fallback_is_bounded_by_bytes(cache: ArtistImageCache, tmp_path: Path) -> None:
    """Bounded on purpose: this exists to stop a broken cache dir multiplying
    upstream traffic, not to turn a disk fault into an OOM. Oldest goes first."""
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    from app.artwork.cache import _FALLBACK_MAX_BYTES

    chunk = _FALLBACK_MAX_BYTES // 4 + 1  # 4 of these overflow the budget
    os.chmod(tmp_path, 0o500)
    try:
        for i in range(4):
            cache.store_positive(f"Artist{i}", b"x" * chunk, "image/png")
        survivors = [i for i in range(4) if isinstance(cache.get(f"Artist{i}"), CachedImage)]
    finally:
        os.chmod(tmp_path, 0o755)

    assert survivors == [1, 2, 3], "the oldest entry must be the one evicted"


# --- the memory tier: its own validator tag, and the lazy write-back ---------


def test_a_stranded_image_carries_its_own_revalidation_tag(tmp_path: Path) -> None:
    """A strand must be validatable, or every request for it skips the 304 path.

    That was the whole bug: ``validator`` stat'd disk only, so a stranded
    portrait looked unvalidatable, fell through to the background filler, and
    each completed fill armed an unscoped ``art:changed`` that re-requested it —
    a self-sustaining remount loop, plus a per-request sha256 and thumb
    re-derive. The dir here is genuinely broken, so nothing can write back and
    the tag is the tier's own.
    """
    cache = ArtistImageCache(_broken_cache_dir(tmp_path))
    _strand(cache, "ABBA", b"stranded", "image/png")

    tag = cache.validator("ABBA")

    assert tag is not None
    # QUOTED STRONG, because app.etag's size_scoped_etag splices its "-t" thumb
    # marker INSIDE the closing quote — an unquoted tag would silently break it.
    assert tag.startswith('"')
    assert tag.endswith('"')
    assert cache.validator("ABBA") == tag, "an unchanged entry must not move its tag"


def test_a_memory_tag_is_never_mistaken_for_a_disk_tag(cache: ArtistImageCache) -> None:
    """Same key, same bytes, two tiers -> two tags.

    A shared tag would 304 a request against the wrong tier's copy, and the two
    families have to stay disjoint by construction (a stat tag opens with a
    digit; a memory tag opens with its own marker).
    """
    _strand(cache, "ABBA", b"same-bytes", "image/png")
    memory_tag = cache.validator("ABBA")

    cache.store_positive("ABBA", b"same-bytes", "image/png")
    disk_tag = cache.validator("ABBA")

    assert memory_tag is not None
    assert disk_tag is not None
    assert disk_tag != memory_tag
    # Disjoint BY CONSTRUCTION, not by luck: get_thumb reads exactly this to
    # decide whether the pair it is about to write can survive a restart, so a
    # memory tag that no longer announces itself is a silent orphan factory.
    assert _is_memory_tag(memory_tag)
    assert not _is_memory_tag(disk_tag)


def test_a_disk_slot_outranks_the_memory_tier_in_the_validator(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """Disk keeps authority the instant it has anything, here as everywhere else.

    Probing memory first would let a strand's tag validate an image ``get()``
    is not about to serve — the two orders must agree.
    """
    key = cache._key("ABBA")
    _strand(cache, "ABBA", b"stranded", "image/png")
    (tmp_path / f"{key}.bin").write_bytes(b"on-disk")

    assert cache.validator("ABBA") == stat_etag(tmp_path / f"{key}.bin")


def test_touching_a_strand_writes_it_back_and_releases_it(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """The persistence half: a repaired dir re-persists on the next TOUCH.

    ``store_positive`` cannot do this — it runs only after ``get()`` misses, and
    a strand makes ``get()`` hit — so without a write-back here the key stayed
    memory-only for the life of the process however healthy the dir became.
    """
    key = cache._key("ABBA")
    _strand(cache, "ABBA", b"stranded", "image/jpeg")
    memory_tag = cache.validator("ABBA")

    got = cache.get("ABBA")

    assert isinstance(got, CachedImage)
    assert got.data == b"stranded"
    assert (tmp_path / f"{key}.bin").read_bytes() == b"stranded"
    assert (tmp_path / f"{key}.mime").read_text(encoding="utf-8") == "image/jpeg"
    assert cache._memory.get(key) is None, "disk took it, so the tier must let go"
    disk_tag = cache.validator("ABBA")
    assert disk_tag is not None
    assert disk_tag != memory_tag


def test_the_write_back_supersedes_a_negative_marker_the_way_a_store_does(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """A strand is a POSITIVE, so persisting it clears any ``.miss`` beneath it.

    Invoked directly because ``get()`` cannot stage this state: a FRESH marker
    short-circuits to NEGATIVE before the memory tier is consulted, and a stale
    one is swept by ``get()`` itself — so a marker reaching the write-back is
    only reachable through the shared publish helper. Hand-rolling the two
    writes inside the write-back instead of reusing that helper is what this
    catches.
    """
    key = cache._key("ABBA")
    cache._ensure_dir()
    (tmp_path / f"{key}.miss").write_text(repr(time.time() + 3600), encoding="utf-8")
    entry = _strand(cache, "ABBA", b"stranded", "image/png")

    cache._write_back(key, entry)

    assert not (tmp_path / f"{key}.miss").exists()
    assert cache.get("ABBA") is not NEGATIVE


def test_a_failed_write_back_leaves_the_strand_exactly_as_it_was(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """During the outage the retry just fails, and must cost nothing.

    ``is`` rather than ``==``: re-putting the entry would build an equal one
    while moving it to the END of the eviction order, quietly turning a bounded
    oldest-first map into an access-ordered cache. The byte-budget test cannot
    see that (it touches every key in insertion order, which permutes to the
    same survivors), so identity is the only pin.

    Swallowed but NOT silent: this is the only signal that a repaired-looking
    install is still serving every portrait out of a map that dies with the
    process, and a dropped ``warn_throttled`` here leaves an operator with a
    working app and no trace at all. Throttled, so it is one line per condition
    per interval however many artists the outage strands.
    """
    cache = ArtistImageCache(_broken_cache_dir(tmp_path))
    key = cache._key("ABBA")
    before = _strand(cache, "ABBA", b"stranded", "image/png")

    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        got = cache.get("ABBA")  # must not raise

    assert isinstance(got, CachedImage)
    assert got.data == b"stranded"
    assert cache._memory.get(key) is before
    assert not (tmp_path / "not-a-dir").is_dir(), "nothing may have reached disk"
    records = _artwork_records(caplog)
    assert len(records) == 1
    assert "unwritable" in records[0].getMessage()


def test_a_remembered_negative_never_materialises_a_miss_file(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """Negatives are excluded from the write-back on purpose.

    They carry their own expiry and self-heal on TTL, and a ``.miss`` conjured
    out of a mere probe would outlive that TTL's intent by barring the
    re-resolve that repairs the key. The dir here is writable, so an absent
    marker is a decision, not a refusal.
    """
    cache._memory.put(cache._key("Nobody"), _NegativeUntil(expiry=time.time() + 3600))

    assert cache.has_fresh_negative("Nobody") is True
    assert cache.get("Nobody") is NEGATIVE
    assert not list(tmp_path.glob("*.miss"))
    # And a negative is not a portrait: nothing for a conditional GET to match.
    assert cache.validator("Nobody") is None


def test_a_fresh_disk_negative_silences_the_memory_tag(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """``get()`` ranks a fresh ``.miss`` above the tier, so the tag must too.

    One process cannot stage this pair (a strand blocks the resolve that
    writes markers), but the backfill daemon runs its own cache instance over
    the same dir and can conclude "no image upstream" while this instance
    still holds a strand. A memory tag answered here would keep 304ing the
    stale portrait for exactly the clients that cached it, while everyone
    else 404s off ``has_fresh_negative`` — two truths from one URL.
    """
    key = cache._key("ABBA")
    _strand(cache, "ABBA", b"stranded", "image/png")
    cache._ensure_dir()
    (tmp_path / f"{key}.miss").write_text(repr(time.time() + 3600), encoding="utf-8")

    assert cache.validator("ABBA") is None
    assert cache.get("ABBA") is NEGATIVE, "the two reads must tell the same story"


def test_a_stale_disk_negative_leaves_the_memory_tag_standing(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """Only a FRESH marker outranks the tier — a lapsed one is already dead."""
    key = cache._key("ABBA")
    entry = _strand(cache, "ABBA", b"stranded", "image/png")
    cache._ensure_dir()
    (tmp_path / f"{key}.miss").write_text(repr(time.time() - 1), encoding="utf-8")

    assert cache.validator("ABBA") == entry.tag


def test_a_reset_during_the_write_back_stays_reset(
    cache: ArtistImageCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish must not resurrect an image the user just cleared.

    The write-back publishes outside the map's lock, so a ``clear_auto``
    landing mid-publish discards the strand AFTER the read but BEFORE the
    let-go. The let-go is an identity compare exactly so this interleaving is
    detectable — and the freshly published files are taken back rather than
    outliving the reset with no TTL.
    """
    key = cache._key("ABBA")
    _strand(cache, "ABBA", b"stranded", "image/png")
    real_publish = cache._publish_positive

    def race(publish_key: str, image: CachedImage) -> None:
        real_publish(publish_key, image)
        cache._memory.discard(publish_key)  # the reset, landing mid-publish

    monkeypatch.setattr(cache, "_publish_positive", race)
    got = cache.get("ABBA")

    assert isinstance(got, CachedImage), "the toucher itself still gets the bytes"
    assert not (tmp_path / f"{key}.bin").exists(), "the reset must stay reset"
    assert not (tmp_path / f"{key}.mime").exists()
    assert cache._memory.get(key) is None


def test_a_newer_strand_survives_a_stale_write_back(
    cache: ArtistImageCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strand replaced mid-publish keeps its newer bytes in the tier.

    An unconditional discard here would drop the replacement along with the
    stale entry — the identity compare is what tells "disk took MINE" apart
    from "somebody newer moved in while I published".
    """
    key = cache._key("ABBA")
    _strand(cache, "ABBA", b"stale", "image/png")
    real_publish = cache._publish_positive

    def race(publish_key: str, image: CachedImage) -> None:
        real_publish(publish_key, image)
        cache._memory.put(publish_key, CachedImage(data=b"newer", content_type="image/png"))

    monkeypatch.setattr(cache, "_publish_positive", race)
    cache.get("ABBA")

    survivor = cache._memory.get(key)
    assert isinstance(survivor, _MemoryEntry)
    assert survivor.image.data == b"newer"
    assert not (tmp_path / f"{key}.bin").exists(), "the stale publish was taken back"


def test_rename_carries_the_strand_without_re_tagging(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """The migrated entry is the SAME object — tag and all, no re-hash.

    The tag is content-derived, so a re-mint would be equal anyway; identity
    is asserted because it is the only observable proof the up-to-10MB hash
    was not spent inside ``rename``.
    """
    entry = _strand(cache, "Fayrouz", b"stranded", "image/png")

    assert cache.rename("Fayrouz", "Fairuz") == "moved"
    assert cache._memory.get(cache._key("Fairuz")) is entry
    assert cache._memory.get(cache._key("Fayrouz")) is None


def test_a_stranded_source_serves_a_thumb_but_caches_no_pair(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """A ``.thumb`` pair keyed to a memory tag is a permanent orphan.

    No restart can reproduce the tag, so nothing would ever match the pair
    again, and ``clear_auto`` only sweeps keys somebody resets. Deriving per
    request while the strand lasts is what the strand already cost.
    """
    key = cache._key("ABBA")
    _strand(cache, "ABBA", _png(1000, 1000), "image/png")

    thumb = cache.get_thumb("ABBA")

    assert thumb is not None
    assert thumb.content_type == "image/webp"
    assert Image.open(io.BytesIO(thumb.data)).size == (320, 320)
    assert not list(tmp_path.glob("*.thumb.*"))
    # Non-vacuity: this dir DOES take writes — the source was written back
    # during the same call, so the missing pair is a skip, not a refusal.
    assert (tmp_path / f"{key}.bin").exists()


def test_the_thumb_after_a_write_back_caches_under_the_disk_tag(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """The skip above is for the strand only, and it ends with the strand."""
    _strand(cache, "ABBA", _png(1000, 1000), "image/png")
    assert cache.get_thumb("ABBA") is not None  # strand: derived, nothing cached
    assert not list(tmp_path.glob("*.thumb.*"))

    assert cache.get_thumb("ABBA") is not None  # source is on disk now

    (src_path,) = tmp_path.glob("*.thumb.src")
    stored_tag, _, _ = src_path.read_text(encoding="utf-8").partition(" ")
    assert stored_tag == cache.validator("ABBA")
    # And the sweep that owns derived thumbs still reaches it.
    cache.clear_auto("ABBA")
    assert not list(tmp_path.glob("*.thumb.*"))


def _touch_one_strand_from_two_threads(
    run_dir: Path, data: bytes
) -> tuple[ArtistImageCache, list[object]]:
    """Two real threads calling ``get()`` on one strand, released together.

    A module-level helper rather than a closure in the loop below: the barrier,
    the result list and the cache would all be loop variables captured by the
    thread body, which is the shape ruff's B023 exists to stop.
    """
    cache = ArtistImageCache(run_dir)
    _strand(cache, "ABBA", data, "image/png")
    start = threading.Barrier(2)
    served: list[object] = []
    append_lock = threading.Lock()

    def touch() -> None:
        start.wait()
        got = cache.get("ABBA")
        with append_lock:
            served.append(got)

    threads = [threading.Thread(target=touch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return cache, served


def test_two_concurrent_touches_of_one_strand_never_destroy_it(tmp_path: Path) -> None:
    """The claim, under real threads. Without it this loses the entry outright.

    Both touches publish (idempotent, so disk is fine) and then both let go:
    the first ``discard_if`` wins and drops the strand, the second finds the key
    gone and cannot tell that from a concurrent ``clear_auto`` — so it takes
    back the files its rival just published. Disk empty, memory empty, a full
    upstream re-resolve, for a key that was never reset. Reachable from one
    artist row: ``?size=full`` and ``?size=thumb`` land in the threadpool
    together.

    Measured 40/40 destroyed on the unclaimed code with exactly this shape, so
    twenty iterations is far past the escape rate; the barrier is what makes
    each one a real race rather than two sequential calls.

    A non-claimer may legitimately observe a transient MISS: its disk probe can
    run before the rival's publish lands and its memory read after the rival's
    release — the entry moved tiers between its two looks. The endpoint heals
    that inside the same request (the filler's own ``get()`` then hits disk),
    so the pin here is "never a wrong answer and never a destroyed entry",
    not "every racer gets bytes".
    """
    data = b"stranded" * 1000
    for iteration in range(20):
        run_dir = tmp_path / f"run{iteration}"
        run_dir.mkdir()

        cache, served = _touch_one_strand_from_two_threads(run_dir, data)

        key = cache._key("ABBA")
        answers = [got.data for got in served if isinstance(got, CachedImage)]
        assert answers, f"iteration {iteration}: nobody got the image"
        assert all(a == data for a in answers)
        assert all(isinstance(got, CachedImage) or got is None for got in served), (
            "a racer saw something other than the image or a transient miss"
        )
        on_disk = (run_dir / f"{key}.bin").exists()
        in_memory = cache._memory.get(key) is not None
        assert on_disk or in_memory, f"iteration {iteration}: the entry was destroyed"
        # The state a healthy dir must reach: disk took it, so the tier let go.
        assert on_disk, f"iteration {iteration}: the bytes never reached disk"
        assert (run_dir / f"{key}.bin").read_bytes() == data
        assert not in_memory, f"iteration {iteration}: disk took it, so the tier must let go"


def test_only_one_caller_at_a_time_claims_a_strands_write_back(
    cache: ArtistImageCache,
) -> None:
    """The claim itself, deterministically — the hammer above proves the effect.

    Three properties, because each is a separate way to reintroduce the bug: a
    second claim on a live one is refused; releasing re-opens the key (a leaked
    claim would bar it from ever persisting again); and a claim for an entry the
    map no longer holds is refused, so a caller that read the strand before a
    replacement cannot publish stale bytes over the newer ones.
    """
    key = cache._key("ABBA")
    entry = _strand(cache, "ABBA", b"stranded", "image/png")

    assert cache._memory.begin_write_back(key, entry) is True
    assert cache._memory.begin_write_back(key, entry) is False, "already in flight"

    cache._memory.end_write_back(key)
    assert cache._memory.begin_write_back(key, entry) is True, "the release must re-open it"
    cache._memory.end_write_back(key)

    replacement = _strand(cache, "ABBA", b"newer", "image/png")
    assert replacement is not entry
    assert cache._memory.begin_write_back(key, entry) is False, "a replaced entry is not claimable"
    assert cache._memory.begin_write_back(key, replacement) is True


def test_a_replaced_strand_mints_a_new_tag(cache: ArtistImageCache) -> None:
    """The tag is DERIVED from the bytes, and a constant would be worse than none.

    A strand replaced after a reset (dir still broken) that kept the old tag
    would 304 every client holding it — permanently serving the portrait the
    reset was meant to remove, to exactly the clients that had cached it.
    """
    first = _strand(cache, "ABBA", b"first-bytes", "image/png")
    assert cache.validator("ABBA") == first.tag

    second = _strand(cache, "ABBA", b"second-bytes", "image/png")

    assert second.tag != first.tag
    assert cache.validator("ABBA") == second.tag


def test_the_tag_moves_when_only_the_content_type_does(cache: ArtistImageCache) -> None:
    """Same bytes, different type — still a different response, so a different tag.

    The digest covers the content-type as well as the payload; a digest over the
    bytes alone would 304 a client holding the image/png copy against an
    image/jpeg one and leave it with a permanently mislabelled portrait.
    """
    png = _strand(cache, "ABBA", b"identical-bytes", "image/png")
    jpeg = _strand(cache, "ABBA", b"identical-bytes", "image/jpeg")

    assert png.tag != jpeg.tag


def test_a_memory_tag_has_the_shape_its_docstring_documents(cache: ArtistImageCache) -> None:
    """``"mem-<16 hex>-<len>"``, pinned including the LENGTH suffix.

    The suffix is half of what the docstring promises and is otherwise
    unobservable: dropping it leaves a tag that still changes with the bytes, so
    every other assertion in this file survives. Matched structurally rather
    than recomputed — a test that re-derives the digest would pass against any
    derivation the production code happened to use.
    """
    data = b"stranded-bytes"
    entry = _strand(cache, "ABBA", data, "image/png")

    match = re.fullmatch(r'"mem-([0-9a-f]{16})-(\d+)"', entry.tag)

    assert match is not None, f"tag {entry.tag!r} is not the documented shape"
    assert int(match.group(2)) == len(data), "the length suffix must be the payload's"


def test_a_strand_still_validates_when_the_cache_dir_becomes_unreadable(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """``validator`` runs FIRST on every image request, strand or no strand.

    Its memory branch re-probes ``.miss`` (a fresh marker outranks the tier), and
    that probe is an ``exists()`` on the broken dir — EACCES after a container
    recreate re-chowns the volume. Unguarded it 500s both image endpoints from
    the very first call, before any other guard can catch it. The sibling test
    below covers the same dir with NO strand, so its ``validator`` returns
    before this branch and never executes it.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: mode 000 does not deny access")
    entry = _strand(cache, "ABBA", b"stranded", "image/png")
    os.chmod(tmp_path, 0o000)
    try:
        tag = cache.validator("ABBA")  # must not raise
    finally:
        os.chmod(tmp_path, 0o755)

    assert tag == entry.tag


def test_re_putting_a_key_counts_its_bytes_once(cache: ArtistImageCache) -> None:
    """The pre-insert discard is what refunds the outgoing entry's bytes.

    Without it a replaced entry's bytes are never given back, so the budget
    shrinks on every re-put and the tier starts evicting live entries early —
    invisible until the map is smaller than it says it is.
    """
    _strand(cache, "ABBA", b"stranded", "image/png")
    _strand(cache, "ABBA", b"stranded-again-and-longer", "image/png")

    assert cache._memory._bytes == len(b"stranded-again-and-longer")


def test_an_unreadable_cache_dir_degrades_instead_of_raising(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """Every read path has to survive a cache dir it cannot open.

    ``Path.exists()`` only swallows ENOENT/ENOTDIR/EBADF/ELOOP, so EACCES —
    an ordinary self-hosted failure, e.g. a container recreate that re-chowns
    the volume, or a stale network mount — propagated out of ``validator``,
    ``get`` AND ``get_thumb`` and 500'd both image endpoints. Writes must not
    raise either: the caller already holds the image it was going to cache.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: mode 000 does not deny access")
    cache.store_positive("ABBA", b"image-bytes", "image/png")
    os.chmod(tmp_path, 0o000)
    try:
        assert cache.validator("ABBA") is None
        assert cache.get("ABBA") is None
        assert cache.get_thumb("ABBA") is None
        cache.store_positive("ABBA", b"new", "image/png")  # must not raise
        cache.store_negative("ABBA", ttl_seconds=60)  # must not raise
    finally:
        os.chmod(tmp_path, 0o755)


def test_get_thumb_still_serves_when_the_cache_dir_is_read_only(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """The thumb is already derived by the time it is written back, so an
    unwritable dir must cost the CACHING, not the image."""
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    cache.store_positive("ABBA", _png(400, 400), "image/png")
    os.chmod(tmp_path, 0o500)
    try:
        thumb = cache.get_thumb("ABBA")
    finally:
        os.chmod(tmp_path, 0o755)

    assert thumb is not None
    assert thumb.content_type == "image/webp"
    assert not list(tmp_path.glob("*.thumb.bin"))  # nothing was cached


def test_clear_auto_removes_the_positive_slot_and_the_marker(tmp_path: Path) -> None:
    cache = ArtistImageCache(tmp_path)
    # ORDER MATTERS: store_positive unlinks .miss, so the marker must be written
    # second or this test's ".miss is gone" assertion proves nothing.
    cache.store_positive("ABBA", b"auto-bytes", "image/png")
    cache.store_negative("ABBA", ttl_seconds=3600)
    key = cache._key("ABBA")
    assert (tmp_path / f"{key}.bin").exists()
    assert (tmp_path / f"{key}.miss").exists()
    assert cache.clear_auto("ABBA") is True
    assert cache.get("ABBA") is None
    assert not (tmp_path / f"{key}.bin").exists()
    assert not (tmp_path / f"{key}.mime").exists()
    assert not (tmp_path / f"{key}.miss").exists()


def test_clear_auto_reports_the_image_slot_never_a_sidecar(tmp_path: Path) -> None:
    """The answer must be "the IMAGE went away", not "some file went away".

    Task 7 turns this bool into the reset endpoint's answer, so a ``clear_auto``
    that reported a sidecar's outcome would claim a reset that never happened --
    the same quiet lie this feature exists to end. Both halves are needed: the
    orphan proves a sidecar cannot manufacture a True, the bare slot proves a
    missing sidecar cannot suppress one.

    The orphaned ``.mime`` is reachable, not hypothetical: ``store_positive``
    publishes the sidecar BEFORE the bytes, so a crash between the two leaves
    exactly this state (``test_store_positive_crash_before_publish_...`` builds
    it deliberately).
    """
    cache = ArtistImageCache(tmp_path)

    orphan = cache._key("Sidecar Only")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{orphan}.mime").write_text("image/png", encoding="utf-8")
    assert cache.clear_auto("Sidecar Only") is False
    assert not (tmp_path / f"{orphan}.mime").exists()  # still swept, just not reported

    bare = cache._key("Bytes Only")
    (tmp_path / f"{bare}.bin").write_bytes(b"image-bytes")
    assert cache.clear_auto("Bytes Only") is True
    assert not (tmp_path / f"{bare}.bin").exists()


def test_clear_override_reports_the_image_slot_never_a_sidecar(tmp_path: Path) -> None:
    """Same guarantee on the override pair, reachable the same way: ``.override``
    is published after ``.override.mime``, so a crash between them leaves an
    orphaned sidecar that must not read as "an override was removed"."""
    cache = ArtistImageCache(tmp_path)

    orphan = cache._key("Sidecar Only")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{orphan}.override.mime").write_text("image/png", encoding="utf-8")
    assert cache.clear_override("Sidecar Only") is False
    assert not (tmp_path / f"{orphan}.override.mime").exists()

    bare = cache._key("Bytes Only")
    (tmp_path / f"{bare}.override").write_bytes(b"image-bytes")
    assert cache.clear_override("Bytes Only") is True
    assert not (tmp_path / f"{bare}.override").exists()


def test_the_clear_calls_unlink_the_image_before_its_mime_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror-image of the write order, and now worth pinning rather than only
    documenting: both clears run through one ``_clear_slots`` loop, so hoisting
    the sweep above the image is a single-line edit that no other test notices.

    Sweeping the sidecar first opens a window where a concurrent ``get()`` finds
    image bytes with no mime and serves them as ``application/octet-stream``.
    Order is not observable through behaviour here -- the window is between two
    syscalls -- so the call sequence itself is the assertion.
    """
    cache = ArtistImageCache(tmp_path)
    cache.store_positive("ABBA", b"auto", "image/png")
    cache.write_override("ABBA", b"manual", "image/png")
    key = cache._key("ABBA")

    order: list[str] = []
    real_unlink = ArtistImageCache._unlink

    def spy(self: ArtistImageCache, path: Path) -> bool:
        order.append(path.name)
        return real_unlink(self, path)

    monkeypatch.setattr(ArtistImageCache, "_unlink", spy)
    cache.clear_auto("ABBA")
    cache.clear_override("ABBA")

    assert order.index(f"{key}.bin") < order.index(f"{key}.mime")
    assert order.index(f"{key}.override") < order.index(f"{key}.override.mime")


def test_clear_auto_drops_a_fresh_negative_marker_so_the_next_call_re_resolves(
    tmp_path: Path,
) -> None:
    cache = ArtistImageCache(tmp_path)
    cache.store_negative("Nobody", ttl_seconds=3600)
    assert cache.get("Nobody") is NEGATIVE
    # No .bin existed, so nothing was "cleared", but the marker must still go.
    assert cache.clear_auto("Nobody") is False
    assert cache.get("Nobody") is None


def test_clear_auto_leaves_a_manual_override_alone(tmp_path: Path) -> None:
    cache = ArtistImageCache(tmp_path)
    cache.store_positive("ABBA", b"auto", "image/png")
    cache.write_override("ABBA", b"manual", "image/jpeg")
    assert cache.clear_auto("ABBA") is True
    cached = cache.get("ABBA")
    assert isinstance(cached, CachedImage)
    assert cached.data == b"manual"


def test_clear_auto_removes_the_derived_thumb(tmp_path: Path) -> None:
    cache = ArtistImageCache(tmp_path)
    cache.store_positive("ABBA", _png(600, 600), "image/png")
    assert cache.get_thumb("ABBA") is not None
    key = cache._key("ABBA")
    assert (tmp_path / f"{key}.thumb.bin").exists()
    cache.clear_auto("ABBA")
    assert not (tmp_path / f"{key}.thumb.bin").exists()
    assert not (tmp_path / f"{key}.thumb.src").exists()


def test_clear_auto_forgets_an_in_memory_fallback_entry(tmp_path: Path) -> None:
    """A cache dir that refused the write keeps the image in the bounded memory
    map. A reset that only unlinked files would keep serving it forever.

    TWO things this had to be rebuilt for. Its dir was named "unwritable" but
    was an ordinary missing path any ``mkdir`` creates, so once ``get()`` gained
    the lazy write-back the setup ``get()`` emptied the map before ``clear_auto``
    ever ran and the test passed for the wrong reason. It now uses a dir that
    genuinely refuses (root included, unlike ``chmod``), and asserts the MAP
    rather than only ``get()`` — with a writable dir the two are no longer the
    same question.
    """
    cache = ArtistImageCache(_broken_cache_dir(tmp_path))
    key = cache._key("ABBA")
    _strand(cache, "ABBA", b"remembered", "image/png")
    assert isinstance(cache.get("ABBA"), CachedImage)

    cache.clear_auto("ABBA")

    assert cache._memory.get(key) is None
    assert cache.get("ABBA") is None


def test_clear_auto_forgets_an_in_memory_negative_marker(tmp_path: Path) -> None:
    """The stand-in holds NEGATIVES too, and a reset has to drop those as well.

    Companion to the positive case above: on a cache dir that refused the write,
    an unexpired in-memory ``.miss`` stand-in answers NEGATIVE, so a reset that
    left it behind would keep short-circuiting the re-resolve for the rest of
    the TTL — the same lie, just told with a "no image" instead of an image.
    """
    if os.getuid() == 0:
        pytest.skip("running as root: a read-only dir does not deny writes")
    cache = ArtistImageCache(tmp_path)
    os.chmod(tmp_path, 0o500)
    try:
        cache.store_negative("Nobody", ttl_seconds=3600)
        assert cache.get("Nobody") is NEGATIVE  # remembered, not on disk
        cache.clear_auto("Nobody")
        assert cache.get("Nobody") is None
    finally:
        os.chmod(tmp_path, 0o755)


def test_clear_override_reports_whether_it_removed_anything(tmp_path: Path) -> None:
    cache = ArtistImageCache(tmp_path)
    assert cache.clear_override("ABBA") is False
    cache.write_override("ABBA", b"manual", "image/png")
    assert cache.clear_override("ABBA") is True
    assert cache.get("ABBA") is None


def test_clear_calls_never_raise_on_an_unremovable_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # EACCES after a container recreate re-chowned the volume: report False,
    # never 500 the endpoint.
    cache = ArtistImageCache(tmp_path)
    cache.store_positive("ABBA", b"auto", "image/png")
    cache.write_override("ABBA", b"manual", "image/png")

    def boom(self: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", boom)
    assert cache.clear_auto("ABBA") is False
    assert cache.clear_override("ABBA") is False


def test_a_refused_unlink_is_reported_once_not_per_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refusal has to leave a trace — silently answering False would make an
    unwritable cache dir indistinguishable from "there was nothing to clear" —
    but ONE trace, not one per slot file: ``clear_auto`` probes five of them and
    a broken dir refuses every one, so an unthrottled record turns a single fact
    into five identical lines per reset."""
    cache = ArtistImageCache(tmp_path)
    cache.store_positive("ABBA", b"auto", "image/png")

    def boom(self: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", boom)
    with caplog.at_level(logging.WARNING, logger="musicdrop.artwork"):
        assert cache.clear_auto("ABBA") is False

    assert len(_artwork_records(caplog)) == 1


def test_has_fresh_negative_is_false_when_nothing_is_cached(tmp_path: Path) -> None:
    assert ArtistImageCache(tmp_path).has_fresh_negative("ABBA") is False


def test_has_fresh_negative_is_true_inside_the_ttl(tmp_path: Path) -> None:
    cache = ArtistImageCache(tmp_path)
    cache.store_negative("Nobody", ttl_seconds=3600)
    assert cache.has_fresh_negative("Nobody") is True


def test_has_fresh_negative_is_false_once_the_ttl_lapses(tmp_path: Path) -> None:
    cache = ArtistImageCache(tmp_path)
    cache.store_negative("Nobody", ttl_seconds=0)
    assert cache.has_fresh_negative("Nobody") is False


def test_has_fresh_negative_honours_an_in_memory_marker(tmp_path: Path) -> None:
    # A cache dir that refused the write keeps the marker in memory; ignoring it
    # would re-fetch a confirmed no-match on every single request, forever.
    cache = ArtistImageCache(tmp_path / "unwritable")
    cache._memory.put(cache._key("Nobody"), _NegativeUntil(expiry=time.time() + 3600))
    assert cache.has_fresh_negative("Nobody") is True


def test_has_fresh_negative_lets_an_expired_in_memory_marker_go(tmp_path: Path) -> None:
    """The stand-in carries the TTL, not just the fact, so an expired one must
    read as "no marker" exactly as an expired ``.miss`` body does — otherwise a
    cache dir that refused ONE write bars that artist from ever re-resolving
    again for the life of the process."""
    cache = ArtistImageCache(tmp_path / "unwritable")
    cache._memory.put(cache._key("Nobody"), _NegativeUntil(expiry=time.time() - 1))
    assert cache.has_fresh_negative("Nobody") is False


def test_has_fresh_negative_never_raises_on_an_unreadable_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ArtistImageCache(tmp_path)

    def boom(self: Path) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "exists", boom)
    assert cache.has_fresh_negative("ABBA") is False


# --- rename (portrait cache re-key) ----------------------------------------


def _slot_files(tmp_path: Path) -> set[str]:
    return {p.name for p in tmp_path.iterdir() if not p.name.endswith(".tmp")}


def test_rename_moves_every_slot_to_the_new_key(cache: ArtistImageCache, tmp_path: Path) -> None:
    cache.store_positive("Fayrouz", b"portrait", "image/jpeg")
    assert cache.rename("Fayrouz", "Queen Fairuz") == "moved"
    got = cache.get("Queen Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"portrait"
    assert got.content_type == "image/jpeg"
    assert cache.get("Fayrouz") is None
    # Nothing remains under the old key on disk.
    old_key = cache._key("Fayrouz")
    assert not any(name.startswith(old_key) for name in _slot_files(tmp_path))


def test_rename_moves_a_manual_override(cache: ArtistImageCache, tmp_path: Path) -> None:
    cache.write_override("Fayrouz", b"pinned", "image/png")
    assert cache.rename("Fayrouz", "Queen Fairuz") == "moved"
    got = cache.get("Queen Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"pinned"
    # The mime sidecar moved too — a dropped one would serve the generic type.
    assert got.content_type == "image/png"
    old_key = cache._key("Fayrouz")
    assert not any(name.startswith(old_key) for name in _slot_files(tmp_path))


def test_rename_merge_keeps_the_targets_portrait(cache: ArtistImageCache, tmp_path: Path) -> None:
    cache.store_positive("Fayrouz", b"source", "image/jpeg")
    cache.store_positive("Fairuz", b"target", "image/jpeg")
    assert cache.rename("Fayrouz", "Fairuz") == "kept_target"
    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"target"
    old_key = cache._key("Fayrouz")
    assert not any(name.startswith(old_key) for name in _slot_files(tmp_path))


def test_rename_a_stale_miss_on_the_target_loses_to_a_real_portrait(
    cache: ArtistImageCache,
) -> None:
    cache.store_positive("Fayrouz", b"source", "image/jpeg")
    cache.store_negative("Fairuz", ttl_seconds=3600)
    assert cache.rename("Fayrouz", "Fairuz") == "moved"
    # The target's marker went: without this the assertion below passes too,
    # since get() never reaches a .miss once a portrait exists.
    assert cache.has_fresh_negative("Fairuz") is False
    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"source"


def test_rename_never_carries_a_negative_marker(cache: ArtistImageCache) -> None:
    """A .miss recorded 'sources had nothing for the OLD name'; the new name
    deserves a fresh lookup."""
    cache.store_negative("Fayrouz", ttl_seconds=3600)
    assert cache.rename("Fayrouz", "Fairuz") == "none"
    assert cache.get("Fairuz") is None  # not NEGATIVE: no marker travelled


def test_rename_same_normalized_key_is_a_noop(cache: ArtistImageCache) -> None:
    cache.store_positive("Beyoncé", b"img", "image/jpeg")
    assert cache.rename("Beyoncé", "beyonce") == "moved"
    got = cache.get("beyonce")
    assert isinstance(got, CachedImage)
    assert got.data == b"img"


def test_rename_with_nothing_cached_reports_none(cache: ArtistImageCache) -> None:
    assert cache.rename("Fayrouz", "Fairuz") == "none"


def test_rename_carries_the_memory_fallback_entry(cache: ArtistImageCache) -> None:
    """An image stranded in the in-memory fallback (broken cache dir at write
    time) must follow the rename too, or it is orphaned exactly like a file."""
    cache._memory.put(cache._key("Fayrouz"), CachedImage(data=b"mem", content_type="image/png"))
    assert cache.rename("Fayrouz", "Fairuz") == "moved"
    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"mem"
    assert cache.get("Fayrouz") is None


def test_rename_merge_source_override_outranks_target_auto(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """A user-pinned portrait must not lose to the target's auto-fetched one."""
    cache.write_override("Fayrouz", b"pinned", "image/png")
    cache.store_positive("Fairuz", b"auto", "image/jpeg")
    assert cache.rename("Fayrouz", "Fairuz") == "moved"
    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"pinned"
    assert got.content_type == "image/png"
    # Clearing the pin reveals the target's auto image again (it was kept beneath).
    assert cache.clear_override("Fairuz") is True
    got2 = cache.get("Fairuz")
    assert isinstance(got2, CachedImage)
    assert got2.data == b"auto"
    old_key = cache._key("Fayrouz")
    assert not any(name.startswith(old_key) for name in _slot_files(tmp_path))


def test_rename_merge_target_override_beats_source_override(cache: ArtistImageCache) -> None:
    cache.write_override("Fayrouz", b"source-pin", "image/png")
    cache.write_override("Fairuz", b"target-pin", "image/png")
    assert cache.rename("Fayrouz", "Fairuz") == "kept_target"
    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"target-pin"


def test_rename_never_raises_when_the_cache_dir_refuses(
    cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.store_positive("Fayrouz", b"portrait", "image/jpeg")

    def boom(src: object, dst: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", boom)
    assert cache.rename("Fayrouz", "Fairuz") == "none"  # degraded, never a 500


def test_every_slot_suffix_is_either_moved_or_deliberately_dropped() -> None:
    assert set(_MOVE_ORDER) | {".miss"} == set(_ALL_SLOT_SUFFIXES)


def test_rename_onto_a_target_that_holds_only_a_strand_keeps_the_targets_bytes(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """A strand IS a portrait for the merge decision — the wrong-artist case.

    ``_has_portrait`` counts the memory tier, so a target whose only copy is a
    strand wins the merge exactly as a disk portrait does. Counting disk alone
    reads the target as empty and takes the move-everything branch instead: the
    SOURCE's ``.bin`` lands on the target key and the target artist starts
    serving somebody else's picture, reported as a clean "moved". That is a
    silent wrong-image bug with no error anywhere, which is why the outcome is
    pinned alongside the verdict.
    """
    cache.store_positive("Fayrouz", b"source-on-disk", "image/jpeg")
    _strand(cache, "Fairuz", b"target-strand", "image/png")

    assert cache.rename("Fayrouz", "Fairuz") == "kept_target"

    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"target-strand", "the target must keep its OWN portrait"
    old_key = cache._key("Fayrouz")
    assert not any(name.startswith(old_key) for name in _slot_files(tmp_path))


def test_a_case_only_rename_of_a_strand_reports_moved(cache: ArtistImageCache) -> None:
    """The same-key branch asks ``_has_portrait``, and a strand is one.

    Nothing moves here (the key is unchanged), so the verdict is the ONLY
    output — and it is not cosmetic: ``api/artists.py`` emits ``art:changed``
    for "moved"/"kept_target" and stays silent on "none", so a strand read as
    "none" leaves every open roster showing the old name's monogram until
    something else bumps the asset version.
    """
    assert cache._key("Beyoncé") == cache._key("beyonce"), "non-vacuity: one key, two spellings"
    _strand(cache, "Beyoncé", b"stranded", "image/png")

    assert cache.rename("Beyoncé", "beyonce") == "moved"

    got = cache.get("beyonce")
    assert isinstance(got, CachedImage)
    assert got.data == b"stranded"


def test_a_rename_chain_never_drifts_the_memory_budget(cache: ArtistImageCache) -> None:
    """``put`` must charge for a CARRIED entry, not only for a fresh image.

    ``put`` converts a ``CachedImage`` to a ``_MemoryEntry`` before it measures,
    so a size test written against ``CachedImage`` matches nothing and charges
    zero — while the discard on the way out still refunds. Each rename then
    drives the budget further negative (measured -32768 after nine), and a
    negative budget silently disables the byte cap the tier exists to enforce.
    Only the accumulator can see this: every ``get()`` still returns the right
    bytes throughout.
    """
    data = b"stranded"
    _strand(cache, "Fayrouz", data, "image/png")

    for old, new in (("Fayrouz", "Fairuz"), ("Fairuz", "Fairouz"), ("Fairouz", "Feyrouz")):
        assert cache.rename(old, new) == "moved"

    assert cache._memory._bytes == len(data)
    carried = cache._memory.get(cache._key("Feyrouz"))
    assert isinstance(carried, _MemoryEntry), "non-vacuity: the entry really did travel"


def test_rename_purging_the_old_key_forgets_its_strand(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """``_purge_old_key`` unlinks five files AND drops the memory entry.

    The old artist has ceased to exist, so a strand left under the old key is
    the forever-orphan ``rename`` exists to prevent — invisible to every disk
    sweep, and served for the life of the process to anyone who asks for the
    old name. The disk half is covered by the merge tests; only this reaches
    the memory half.
    """
    old_key = cache._key("Fayrouz")
    _strand(cache, "Fayrouz", b"source-strand", "image/png")
    cache.store_positive("Fairuz", b"target-on-disk", "image/jpeg")

    assert cache.rename("Fayrouz", "Fairuz") == "kept_target"

    assert cache._memory.get(old_key) is None
    assert cache.get("Fayrouz") is None, "the departed name must serve nothing"
    assert not any(name.startswith(old_key) for name in _slot_files(tmp_path))


def test_rename_moving_a_pin_onto_an_auto_image_forgets_the_sources_strand(
    cache: ArtistImageCache, tmp_path: Path
) -> None:
    """The other target-wins branch drops the old key's strand too.

    ``_rename_pin_onto_auto`` moves only the override pair and deletes the rest
    of the old key — the strand is part of "the rest", and it is the part no
    ``_unlink`` can reach. Left behind it outlives the artist it belonged to.
    """
    old_key = cache._key("Fayrouz")
    cache.write_override("Fayrouz", b"source-pin", "image/png")
    _strand(cache, "Fayrouz", b"source-strand", "image/png")
    cache.store_positive("Fairuz", b"target-auto", "image/jpeg")

    assert cache.rename("Fayrouz", "Fairuz") == "moved"

    assert cache._memory.get(old_key) is None
    assert cache.get("Fayrouz") is None, "the departed name must serve nothing"
    assert not any(name.startswith(old_key) for name in _slot_files(tmp_path))
    # The pin really did land, so the branch under test is the one that ran.
    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"source-pin"


def test_rename_move_failure_returns_kept_target(
    cache: ArtistImageCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the override-pin move fails (broken dir), return 'kept_target'
    because the target's auto image is still available via get()."""
    cache.write_override("Fayrouz", b"pinned", "image/png")
    cache.store_positive("Fairuz", b"auto", "image/jpeg")

    def raise_for_override(src: object, dst: object) -> None:
        src_str = str(src) if not isinstance(src, str) else src
        if src_str.endswith(".override"):
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", raise_for_override)
    result = cache.rename("Fayrouz", "Fairuz")
    assert result == "kept_target"
    # The target's auto image is still available.
    got = cache.get("Fairuz")
    assert isinstance(got, CachedImage)
    assert got.data == b"auto"
