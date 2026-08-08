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
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.artwork.images import FALLBACK_CONTENT_TYPE, is_header_safe_content_type
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

        # Guarded like the slot reads above: Path.exists() re-raises everything
        # outside ENOENT/ENOTDIR/EBADF/ELOOP, so an unreadable cache dir would
        # otherwise 500 the endpoint from here even though both slot reads
        # already survived it.
        miss = self._dir / f"{key}.miss"
        try:
            if miss.exists():
                if time.time() < self._read_expiry(miss):
                    return NEGATIVE
                # Expired (or unparseable): drop the marker so the caller re-resolves.
                miss.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning("artist-image negative marker %s is unreadable: %s", miss.name, exc)

        return None

    def store_positive(self, name: str, data: bytes, content_type: str) -> None:
        key = self._key(name)
        # A cache write may never fail the request that triggered it: the caller
        # already HAS the image and is about to serve it, so an unwritable cache
        # dir (volume re-chowned on recreate, read-only mount, disk full) must
        # cost the caching, not the response. Logged, because "every image
        # re-resolves from the network forever" is otherwise invisible.
        with self._best_effort_write("positive slot", name):
            self._ensure_dir()
            # A positive result supersedes any prior negative marker.
            (self._dir / f"{key}.miss").unlink(missing_ok=True)
            # Write the mime sidecar BEFORE the bytes so a concurrent get() never
            # reads image bytes paired with a missing/stale content-type. Both writes
            # are atomic (tmp + os.replace) so a reader never catches a truncated
            # file and a crash can't leave a corrupt image cached (see _read_image).
            _atomic_write_bytes(self._dir / f"{key}.mime", content_type.encode("utf-8"))
            _atomic_write_bytes(self._dir / f"{key}.bin", data)

    def store_negative(self, name: str, *, ttl_seconds: float) -> None:
        key = self._key(name)
        with self._best_effort_write("negative marker", name):
            self._ensure_dir()
            # Store an absolute expiry; the TTL travels with the marker.
            expiry = time.time() + ttl_seconds
            (self._dir / f"{key}.miss").write_text(repr(expiry), encoding="utf-8")

    @contextmanager
    def _best_effort_write(self, what: str, name: str) -> Iterator[None]:
        """Swallow+log an OSError from a cache write (see ``store_positive``)."""
        try:
            yield
        except OSError as exc:
            _log.warning("artist-image cache could not write the %s for %r: %s", what, name, exc)

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
        """One slot's bytes + content-type, or None when the slot is unusable.

        Nothing in here may raise, and nothing it returns may be unsendable.
        Both image endpoints call this through ``get()`` with no guard of their
        own, and ``get_thumb`` reaches its SOURCE through it too — so its
        "degrades to the original, never a 500" promise is only as airtight as
        this read, and a corrupt sidecar could abort the very self-heal that
        promise exists for.

        Three failure modes, answered differently on purpose:

        * unreadable BYTES read as "nothing cached", so the caller re-resolves
          and the entry rebuilds;
        * an unreadable SIDECAR must not cost the image — it takes the generic
          content-type a missing one does. ``UnicodeDecodeError`` is a
          ``ValueError``, so one guard covers a non-UTF-8 body and a vanished
          file;
        * a sidecar that decodes but holds something that cannot BE a header —
          non-ASCII, or a newline — takes the same fallback. That value comes
          straight off a CDN (see ``is_header_safe_content_type``), so it is a
          poisoned-slot case, not a corrupted-disk one.

        The ``exists()`` calls are guarded rather than trusted: ``Path.exists()``
        only swallows ENOENT/ENOTDIR/EBADF/ELOOP, so EACCES (a container
        recreate that re-chowned the cache volume) or ESTALE (a dropped network
        mount) would otherwise raise from inside a function that promises not
        to — same shape as :func:`_stat_tag`.
        """
        try:
            if not data_path.exists():
                return None
            data = data_path.read_bytes()
        except OSError as exc:
            # exists()-then-read is also a window the backfill daemon's atomic
            # replace — and clear_override's unlink — can move under us.
            _log.warning("artist-image cache slot %s is unreadable: %s", data_path.name, exc)
            return None
        content_type = FALLBACK_CONTENT_TYPE
        try:
            if mime_path.exists():
                content_type = mime_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError) as exc:
            _log.warning(
                "artist-image mime sidecar %s is unreadable, serving the image as %s: %s",
                mime_path.name,
                FALLBACK_CONTENT_TYPE,
                exc,
            )
            content_type = FALLBACK_CONTENT_TYPE
        if not is_header_safe_content_type(content_type):
            # Deliberately NOT logged here. This is a READ of stored state, so a
            # line here repeats on every request for as long as the slot stays
            # poisoned — the per-request spam the degrade log was careful to
            # avoid. The operator signal belongs where the event happens once:
            # download.py warns when such a value is accepted from a CDN, and
            # that boundary now refuses to store one at all.
            content_type = FALLBACK_CONTENT_TYPE
        return CachedImage(data=data, content_type=content_type)

    def validator(self, name: str) -> str | None:
        """Cheap revalidation tag for the image get() would serve: the override
        slot when present, else the positive slot. None when neither exists (miss
        or negative — the caller falls through to the resolve path). Same
        stat-based scheme as the album cover's cover_validator: any writer
        replaces the file (atomic rename bumps mtime), so a stale tag can never
        yield a false 304.

        The probe is guarded for the same reason :func:`_stat_tag` is: this runs
        FIRST on every image request, so an unreadable cache dir (EACCES after a
        volume re-chown, ESTALE on a dropped mount) raising out of ``exists()``
        here would 500 both endpoints before any other guard could catch it.
        Unreadable reads as "nothing to validate" — the caller falls through to
        its resolve path exactly as it does on a cache miss."""
        key = self._key(name)
        try:
            for slot in (f"{key}.override", f"{key}.bin"):
                path = self._dir / slot
                if path.exists():
                    return _stat_tag(path)
        except OSError as exc:
            _log.warning("artist-image cache dir is unreadable (%s); serving unvalidated", exc)
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
            # A stored mime that cannot BE a header (non-ASCII, embedded
            # newline) misses on purpose rather than falling back: re-deriving
            # replaces it with this cache's own THUMB_MIME, which is strictly
            # better than serving a WebP labelled application/octet-stream.
            if (
                stored_tag == src_tag
                and is_header_safe_content_type(stored_mime)
                and thumb_path.exists()
            ):
                return CachedImage(data=thumb_path.read_bytes(), content_type=stored_mime)
        except (OSError, ValueError):
            # Missing/unreadable sidecar — rederive below. ValueError covers
            # UnicodeDecodeError (a non-UTF-8 body); OSError covers an
            # unreadable cache dir, which reaches this through exists() too.
            # This cache self-heals, and the image endpoint has no guard that
            # would keep either off the wire.
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
            # trailing space, reads back unusable, and the entry then re-derives
            # on every single request. Second line of defence — _read_image now
            # guarantees a header-safe, non-empty type — kept because get_thumb
            # does not own that invariant. Same guard as CoverThumbCache.
            data, mime = source.data, source.content_type or FALLBACK_CONTENT_TYPE
        # Caching is best-effort: the thumb is already derived, so an unwritable
        # cache dir must cost the caching, not the image. Silent because this is
        # per-request while the dir stays unwritable — store_positive's write of
        # the SAME dir logs it once per resolve.
        with suppress(OSError):
            self._ensure_dir()
            _atomic_write_bytes(thumb_path, data)
            _atomic_write_bytes(src_path, f"{src_tag} {mime}".encode())
        return CachedImage(data=data, content_type=mime)
