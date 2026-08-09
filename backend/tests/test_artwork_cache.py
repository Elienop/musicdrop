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
    assert thumb is not None and thumb.content_type == "image/webp"
    assert Image.open(io.BytesIO(thumb.data)).size == (320, 320)
    # Second call serves the stored derivation (no re-encode): patching
    # make_thumb to explode proves it isn't called again.
    with unittest.mock.patch("app.artwork.degrade.make_thumb", side_effect=AssertionError):
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
    with unittest.mock.patch("app.artwork.degrade.make_thumb", side_effect=AssertionError):
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
    assert thumb is not None and thumb.content_type == "application/octet-stream"
    with unittest.mock.patch("app.artwork.degrade.make_thumb", side_effect=AssertionError):
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

    assert healed is not None and healed.content_type == "image/webp"


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

    assert healed is not None and healed.content_type == "image/webp"


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

    assert served is not None and served.content_type == "image/webp"


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
    assert got.data == b"image-bytes" and got.content_type == "image/png"


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
    assert isinstance(got, CachedImage) and got.data == b"from-disk"

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

    assert thumb is not None and thumb.content_type == "image/webp"
    assert not list(tmp_path.glob("*.thumb.bin"))  # nothing was cached


def test_clear_auto_removes_the_positive_slot_and_the_marker(tmp_path: Path) -> None:
    cache = ArtistImageCache(tmp_path)
    # ORDER MATTERS: store_positive unlinks .miss, so the marker must be written
    # second or this test's ".miss is gone" assertion proves nothing.
    cache.store_positive("ABBA", b"auto-bytes", "image/png")
    cache.store_negative("ABBA", ttl_seconds=3600)
    key = cache._key("ABBA")
    assert (tmp_path / f"{key}.bin").exists() and (tmp_path / f"{key}.miss").exists()
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
    # A cache dir that refused the write keeps the image in the bounded memory
    # map. A reset that only unlinked files would keep serving it forever.
    cache = ArtistImageCache(tmp_path / "unwritable")
    cache._memory.put(cache._key("ABBA"), CachedImage(data=b"remembered", content_type="image/png"))
    assert isinstance(cache.get("ABBA"), CachedImage)
    cache.clear_auto("ABBA")
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
