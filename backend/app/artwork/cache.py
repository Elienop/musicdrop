"""On-disk artist-image cache with override / positive / negative slots.

Layout (under the configured cache dir), keyed by ``sha1(normalized_name)``::

    <key>.override      manual pin (image bytes)   — always wins
    <key>.override.mime content-type of the override
    <key>.bin           auto-fetched image bytes   — positive slot
    <key>.mime          content-type of the positive image
    <key>.miss          negative marker; file mtime is the timestamp (TTL'd)

Pure filesystem; no network. The mime is stored in a sidecar text file so the
binary slot stays a plain image (cheap to ``sendfile`` later).
"""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.artwork.normalize import normalize_artist_name


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


class ArtistImageCache:
    def __init__(self, cache_dir: Path | str, *, negative_ttl_seconds: int) -> None:
        self._dir = Path(cache_dir)
        self._negative_ttl_seconds = negative_ttl_seconds

    def _key(self, name: str) -> str:
        normalized = normalize_artist_name(name)
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, name: str) -> CachedImage | _Negative | None:
        """Resolve the cache for ``name``.

        Returns a :class:`CachedImage` for an override or positive hit,
        :data:`NEGATIVE` for a fresh negative marker, or ``None`` when the
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
            age = time.time() - miss.stat().st_mtime
            if age < self._negative_ttl_seconds:
                return NEGATIVE
            # Stale: drop the marker so the caller re-resolves.
            miss.unlink(missing_ok=True)

        return None

    def store_positive(self, name: str, data: bytes, content_type: str) -> None:
        self._ensure_dir()
        key = self._key(name)
        # A positive result supersedes any prior negative marker.
        (self._dir / f"{key}.miss").unlink(missing_ok=True)
        (self._dir / f"{key}.bin").write_bytes(data)
        (self._dir / f"{key}.mime").write_text(content_type, encoding="utf-8")

    def store_negative(self, name: str) -> None:
        self._ensure_dir()
        key = self._key(name)
        # Touch (or refresh) the miss marker; mtime is the timestamp.
        (self._dir / f"{key}.miss").write_text(str(time.time()), encoding="utf-8")

    def write_override(self, name: str, data: bytes, content_type: str) -> None:
        """Plant a manual override (always wins). No auto-writer in this chunk."""
        self._ensure_dir()
        key = self._key(name)
        (self._dir / f"{key}.override").write_bytes(data)
        (self._dir / f"{key}.override.mime").write_text(content_type, encoding="utf-8")

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
