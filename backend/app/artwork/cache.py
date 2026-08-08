"""On-disk artist-image cache with override / positive / negative slots.

Layout (under the configured cache dir), keyed by ``sha1(normalized_name)``::

    <key>.override      manual pin (image bytes)   — always wins
    <key>.override.mime content-type of the override
    <key>.bin           auto-fetched image bytes   — positive slot
    <key>.mime          content-type of the positive image
    <key>.miss          negative marker; body is an absolute expiry timestamp
    <key>.thumb.bin      derived 320px WebP of the winning slot (or, degraded,
                         a verbatim copy of the source bytes — see get_thumb)
    <key>.thumb.src      "<source validator> <content-type of .thumb.bin>"

Pure filesystem; no network. The mime is stored in a sidecar text file so the
binary slot stays a plain image (cheap to ``sendfile`` later).

Negative caching stores an **absolute expiry** (``now + ttl_seconds``) in the
``.miss`` body rather than relying on file mtime, so the TTL travels with the
marker. Callers pass a short TTL for transient failures and a long one for a
confirmed no-match (see :class:`ArtistImageService`). An unparseable body is
treated as already-stale so a corrupt marker self-heals on the next lookup.
"""

import hashlib
import logging
import os
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.artwork.normalize import normalize_artist_name
from app.artwork.thumbs import THUMB_MIME, ThumbError, make_thumb

_log = logging.getLogger("musicdrop.artwork")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Publish ``data`` to ``path`` atomically: write a unique same-dir tmp,
    fsync it, then ``os.replace``.

    The artist-art backfill daemon writes this cache on its own thread while the
    event loop serves image GETs over the same files, so a plain truncate-then-
    write lets a reader catch a half-written image (served 200 with a content-
    hash ETag) and a crash mid-write leaves a truncated file cached forever (the
    positive slot has no TTL/validation). An atomic rename closes both: a reader
    only ever opens the old or the new whole file, and an interrupted write
    leaves the tmp, never the target. No parent-dir fsync — this cache is
    rebuildable, so per-image rename durability isn't worth an fsync per write on
    the library-wide backfill.
    """
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with suppress(OSError):
                tmp.unlink()


@dataclass(frozen=True)
class CachedImage:
    """A cached image payload: raw bytes plus its content-type."""

    data: bytes
    content_type: str


class _Negative:
    """Sentinel for a fresh negative-cache hit (known no-image)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NEGATIVE"


NEGATIVE: Final[_Negative] = _Negative()


def _stat_tag(path: Path) -> str | None:
    """A strong ETag from mtime_ns+size — no read. None if the file vanished
    (stat races the backfill daemon's atomic replace; the caller just serves
    the full body with a content-hash fallback)."""
    try:
        st = path.stat()
    except OSError:
        return None
    return f'"{st.st_mtime_ns}-{st.st_size}"'


class ArtistImageCache:
    def __init__(self, cache_dir: Path | str) -> None:
        self._dir = Path(cache_dir)

    def _key(self, name: str) -> str:
        normalized = normalize_artist_name(name)
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, name: str) -> CachedImage | _Negative | None:
        """Resolve the cache for ``name``.

        Returns a :class:`CachedImage` for an override or positive hit,
        :data:`NEGATIVE` for an unexpired negative marker, or ``None`` when the
        cache has nothing usable (empty or stale negative).
        """
        key = self._key(name)

        override = self._read_image(
            self._dir / f"{key}.override", self._dir / f"{key}.override.mime"
        )
        if override is not None:
            return override

        positive = self._read_image(self._dir / f"{key}.bin", self._dir / f"{key}.mime")
        if positive is not None:
            return positive

        miss = self._dir / f"{key}.miss"
        if miss.exists():
            if time.time() < self._read_expiry(miss):
                return NEGATIVE
            # Expired (or unparseable): drop the marker so the caller re-resolves.
            miss.unlink(missing_ok=True)

        return None

    def store_positive(self, name: str, data: bytes, content_type: str) -> None:
        self._ensure_dir()
        key = self._key(name)
        # A positive result supersedes any prior negative marker.
        (self._dir / f"{key}.miss").unlink(missing_ok=True)
        # Write the mime sidecar BEFORE the bytes so a concurrent get() never
        # reads image bytes paired with a missing/stale content-type. Both writes
        # are atomic (tmp + os.replace) so a reader never catches a truncated
        # file and a crash can't leave a corrupt image cached (see _read_image).
        _atomic_write_bytes(self._dir / f"{key}.mime", content_type.encode("utf-8"))
        _atomic_write_bytes(self._dir / f"{key}.bin", data)

    def store_negative(self, name: str, *, ttl_seconds: float) -> None:
        self._ensure_dir()
        key = self._key(name)
        # Store an absolute expiry; the TTL travels with the marker.
        expiry = time.time() + ttl_seconds
        (self._dir / f"{key}.miss").write_text(repr(expiry), encoding="utf-8")

    def write_override(self, name: str, data: bytes, content_type: str) -> None:
        """Plant a manual override (always wins). No auto-writer in this chunk."""
        self._ensure_dir()
        key = self._key(name)
        # Mime before bytes, both atomic (see store_positive).
        _atomic_write_bytes(self._dir / f"{key}.override.mime", content_type.encode("utf-8"))
        _atomic_write_bytes(self._dir / f"{key}.override", data)

    def clear_override(self, name: str) -> None:
        """Remove a manual override -> next get() falls back to auto/cache.

        Unlink the BYTES before the MIME (mirror-image of write_override's
        mime-before-bytes order) so a concurrent get() never reads override
        bytes paired with a missing mime sidecar.
        """
        key = self._key(name)
        (self._dir / f"{key}.override").unlink(missing_ok=True)
        (self._dir / f"{key}.override.mime").unlink(missing_ok=True)

    @staticmethod
    def _read_expiry(miss_path: Path) -> float:
        """Absolute expiry stored in the marker; 0.0 (already-stale) if corrupt."""
        try:
            return float(miss_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0.0

    @staticmethod
    def _read_image(data_path: Path, mime_path: Path) -> CachedImage | None:
        if not data_path.exists():
            return None
        data = data_path.read_bytes()
        content_type = (
            mime_path.read_text(encoding="utf-8").strip()
            if mime_path.exists()
            else "application/octet-stream"
        )
        return CachedImage(data=data, content_type=content_type)

    def validator(self, name: str) -> str | None:
        """Cheap revalidation tag for the image get() would serve: the override
        slot when present, else the positive slot. None when neither exists (miss
        or negative — the caller falls through to the resolve path). Same
        stat-based scheme as the album cover's cover_validator: any writer
        replaces the file (atomic rename bumps mtime), so a stale tag can never
        yield a false 304."""
        key = self._key(name)
        for slot in (f"{key}.override", f"{key}.bin"):
            path = self._dir / slot
            if path.exists():
                return _stat_tag(path)
        return None

    def get_thumb(self, name: str) -> CachedImage | None:
        """The 320px WebP derivation of what get() would serve, deriving (and
        caching) it if the stored one is missing or was built from different
        source bytes. None when no source image is cached (the caller resolves
        first, then retries). An undecodable source degrades to the original
        bytes — a grid that shows SOME image beats a 500."""
        src_tag = self.validator(name)
        if src_tag is None:
            return None
        key = self._key(name)
        thumb_path = self._dir / f"{key}.thumb.bin"
        src_path = self._dir / f"{key}.thumb.src"
        try:
            stored = src_path.read_text(encoding="utf-8")
            stored_tag, _, stored_mime = stored.partition(" ")
            if stored_tag == src_tag and stored_mime and thumb_path.exists():
                return CachedImage(data=thumb_path.read_bytes(), content_type=stored_mime)
        except (OSError, ValueError):
            # Missing/unreadable sidecar — rederive below. ValueError covers
            # UnicodeDecodeError (a non-UTF-8 body): this cache self-heals, and
            # the image endpoint has no guard that would keep that off the wire.
            pass
        source = self.get(name)
        if not isinstance(source, CachedImage):
            return None
        try:
            data = make_thumb(source.data)
            mime = THUMB_MIME
        except ThumbError as exc:
            # Degrading is right; being silent about it was not. Before
            # make_thumb's guard was widened to `except Exception`, a SYSTEMIC
            # encoder failure (Pillow built without WebP, a broken format
            # plugin) surfaced as a 500 — loud and diagnosable. Unlogged it
            # would serve every image full-size forever, indistinguishable from
            # the thumb feature never having been deployed.
            #
            # WARNING, not INFO: nothing in this app configures the root logger,
            # so under uvicorn's default config anything below WARNING is
            # dropped entirely. Not spam either — the degraded bytes are stored
            # below, so this fires once per source version, not per request.
            _log.warning("artist-image thumbnail degraded to the original for %r: %s", name, exc)
            # Never store a blank mime: it round-trips through .thumb.src as a
            # trailing space, reads back falsy, and the entry then re-derives on
            # every single request. Same guard as CoverThumbCache.
            data, mime = source.data, source.content_type or "application/octet-stream"
        self._ensure_dir()
        _atomic_write_bytes(thumb_path, data)
        _atomic_write_bytes(src_path, f"{src_tag} {mime}".encode())
        return CachedImage(data=data, content_type=mime)
