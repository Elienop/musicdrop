"""On-disk artist-image cache with override / positive / negative slots.

Layout (under the configured cache dir), keyed by ``sha1(normalized_name)``::

    <key>.override      manual pin (image bytes)   — always wins
    <key>.override.mime content-type of the override
    <key>.bin           auto-fetched image bytes   — positive slot
    <key>.mime          content-type of the positive image
    <key>.miss          negative marker; body is an absolute expiry timestamp

Pure filesystem; no network. The mime is stored in a sidecar text file so the
binary slot stays a plain image (cheap to ``sendfile`` later).

Negative caching stores an **absolute expiry** (``now + ttl_seconds``) in the
``.miss`` body rather than relying on file mtime, so the TTL travels with the
marker. Callers pass a short TTL for transient failures and a long one for a
confirmed no-match (see :class:`ArtistImageService`). An unparseable body is
treated as already-stale so a corrupt marker self-heals on the next lookup.
"""

import hashlib
import os
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.artwork.normalize import normalize_artist_name


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
