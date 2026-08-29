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

**When the directory itself is broken** (a container recreate that re-chowned
the volume, a read-only or full disk) nothing here may raise — the caller is
serving an image it already holds. But swallowing a failed write is not free:
the ``.miss`` marker is the ONLY thing bounding re-fetches, so a silently
dropped one turns a 7-day confirmed-no-match into an upstream call on every
request, forever, invisible. :class:`_MemoryFallback` is what keeps that bounded:
a write that could not reach disk is remembered in a bounded map instead, so a
broken cache dir degrades to a smaller cache rather than to no cache at all. The
map lives on the INSTANCE and for its lifetime — the backfill daemon builds its
own cache object, so each carries its own budget.

That map is a real cache TIER, not just a holding pen, and the two properties
that make it one are load-bearing:

* **it answers ``validator()``.** A stranded image carries its own quoted-strong
  revalidation tag (``"mem-<digest>-<size>"``, computed once when the entry is
  created), so a request for it takes the same 304 / serve-from-cache path a
  disk entry takes. Without that the endpoint saw "nothing to validate", fell
  through to the background filler, and every completed fill armed an unscoped
  ``art:changed`` — a self-sustaining remount/refetch loop for as long as one
  tab showed one stranded artist, plus a sha256 over up to 10 MB and a thumb
  re-derive per request;
* **it writes back lazily, on the next touch.** ``get()`` re-attempts the disk
  write it could not do at store time and drops the entry once disk takes it, so
  a repaired cache dir re-persists without a restart and without a background
  job. A restart before that touch loses the memory copies — accepted by design;
  they simply re-fetch.

Only WRITE FAILURES land here — never a read-through copy of a disk entry — so
disk always has authority and a stale stand-in can never outlive its slot.
"""

import hashlib
import os
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from app.artwork.degrade import derive_thumb_or_degrade, warn_throttled
from app.artwork.images import FALLBACK_CONTENT_TYPE, header_safe_content_type
from app.artwork.normalize import normalize_artist_name
from app.etag import stat_etag

_OVERRIDE_SUFFIX = ".override"
_OVERRIDE_MIME_SUFFIX = ".override.mime"
_BIN_SUFFIX = ".bin"
_MIME_SUFFIX = ".mime"
_CACHE_DIR_UNREADABLE = "artist-image cache dir is unreadable: %s"
# One string for both writers under the "cache-write" throttle key: the two
# sites (_or_remember's failure and the lazy write-back's) must stay
# word-identical or the throttle dedups messages readers cannot correlate.
_CACHE_DIR_UNWRITABLE = (
    "artist-image cache dir is unwritable (%s); "
    "serving from a bounded in-memory fallback until it recovers"
)

# Every slot suffix an artist's key can own (kept in ONE place so rename and
# any future sweep cannot drift from the layout above).
_ALL_SLOT_SUFFIXES: Final = (
    _OVERRIDE_SUFFIX,
    _OVERRIDE_MIME_SUFFIX,
    _BIN_SUFFIX,
    _MIME_SUFFIX,
    ".miss",
    ".thumb.bin",
    ".thumb.src",
)

# Move order for the re-key (sidecar before bytes at the destination);
# ``.miss`` is deliberately absent — a negative marker never travels.
_MOVE_ORDER: Final = (
    _OVERRIDE_MIME_SUFFIX,
    _OVERRIDE_SUFFIX,
    _MIME_SUFFIX,
    _BIN_SUFFIX,
    ".thumb.src",
    ".thumb.bin",
)


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


@dataclass(frozen=True)
class _NegativeUntil:
    """An in-memory stand-in for a ``.miss`` marker that could not be written."""

    expiry: float


#: Marker opening a MEMORY-tier validator tag. A disk tag is ``"{mtime_ns}-{size}"``
#: (see :func:`app.etag.stat_etag`), so it always starts with a digit — this prefix
#: makes the two families disjoint by construction, which is what lets
#: :meth:`ArtistImageCache.get_thumb` tell "derived from a slot a restart can
#: reproduce" from "derived from a strand that dies with the process".
_MEMORY_TAG_MARK: Final = "mem-"


def _memory_tag(image: CachedImage) -> str:
    """The revalidation tag a stranded image serves under.

    QUOTED STRONG, like every tag in :mod:`app.etag`, because
    ``size_scoped_etag`` splices its ``-t`` thumb marker INSIDE the closing
    quote. Derived from the response (content-type + bytes) rather than from a
    clock or a counter so it is stable for an unchanged entry across calls, and
    changes the moment either half of the response does. Computed ONCE, when the
    entry is created: this hashes up to 10 MB and ``validator()`` runs on every
    single image request.
    """
    digest = hashlib.sha256(image.content_type.encode("utf-8") + b"\x00" + image.data).hexdigest()
    return f'"{_MEMORY_TAG_MARK}{digest[:16]}-{len(image.data)}"'


def _is_memory_tag(tag: str) -> bool:
    """Whether ``tag`` came from the memory tier rather than a disk ``stat``."""
    return tag.startswith(f'"{_MEMORY_TAG_MARK}')


@dataclass(frozen=True)
class _MemoryEntry:
    """A stranded image plus the tag it revalidates under, paired for life.

    The tag is stored, not recomputed per ``validator()`` call — see
    :func:`_memory_tag`.
    """

    image: CachedImage
    tag: str


# Bounds for the fallback below. 64 images at the 10 MB upload cap would be
# 640 MB, so the BYTE budget is the real limit and the entry count only keeps
# the bookkeeping small; negatives cost nothing and are not charged against it.
_FALLBACK_MAX_ENTRIES: Final = 512
_FALLBACK_MAX_BYTES: Final = 32 * 1024 * 1024


class _MemoryFallback:
    """Bounded, process-lifetime cache TIER for entries disk refused to take.

    Populated ONLY when a write failed — never as a read-through copy of a disk
    entry — so on a healthy install it stays empty and costs nothing, and disk
    keeps authority whenever disk has anything at all. Insertion-ordered
    eviction (oldest first) under both a byte budget and an entry cap: this
    exists to stop a broken cache dir from multiplying upstream traffic, not to
    become an unbounded second cache that turns a disk problem into an OOM.

    A stored image is a full tier entry, not a spare copy: it carries the
    revalidation tag it serves under (:func:`_memory_tag`), so
    ``ArtistImageCache.validator`` can answer from here and the request takes
    the ordinary 304 / serve-from-cache path. It is released by the lazy
    write-back in ``ArtistImageCache.get`` as soon as disk accepts the entry
    again — see the module docstring.

    Thread-safe because concurrent image GETs run in the threadpool and can
    hit one instance at once. NOT shared with the artist-art backfill daemon:
    that builds its own ArtistImageCache (artist_art_jobs/runner.py), so it
    has a separate fallback and its own budget.
    """

    def __init__(self, *, max_entries: int, max_bytes: int) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _MemoryEntry | _NegativeUntil] = OrderedDict()
        self._bytes = 0

    def put(self, key: str, value: CachedImage | _NegativeUntil | _MemoryEntry) -> None:
        """Remember ``value`` under ``key``, tagging an image on the way in.

        The tag is minted HERE, outside the lock (it hashes the payload), so
        every stored image has one and nothing on the read path ever recomputes
        it. Re-putting the same image mints an equal tag — the digest is over
        the response, not over the insertion. An already-tagged ``_MemoryEntry``
        is carried as-is (rename migrating a strand): the digest is
        content-derived, so re-minting would only spend a hash of up to 10 MB
        to compute the tag the entry already holds.
        """
        entry: _MemoryEntry | _NegativeUntil
        if isinstance(value, CachedImage):
            entry = _MemoryEntry(image=value, tag=_memory_tag(value))
        else:
            entry = value
        with self._lock:
            self._discard_locked(key)
            size = len(entry.image.data) if isinstance(entry, _MemoryEntry) else 0
            if size > self._max_bytes:
                return  # one image larger than the whole budget: keep nothing
            self._entries[key] = entry
            self._bytes += size
            while self._entries and (
                len(self._entries) > self._max_entries or self._bytes > self._max_bytes
            ):
                _, evicted = self._entries.popitem(last=False)
                if isinstance(evicted, _MemoryEntry):
                    self._bytes -= len(evicted.image.data)

    def get(self, key: str) -> _MemoryEntry | _NegativeUntil | None:
        with self._lock:
            return self._entries.get(key)

    def discard(self, key: str) -> None:
        """Forget ``key`` — disk took it (or its marker expired), so disk wins."""
        with self._lock:
            self._discard_locked(key)

    def discard_if(self, key: str, entry: _MemoryEntry) -> bool:
        """Forget ``key`` only while it still holds exactly ``entry`` (identity).

        The lazy write-back publishes to disk OUTSIDE the lock, so by the time
        it lets go of the strand a concurrent ``clear_auto`` may have discarded
        it (a user reset the publish must not undo) or a fresh strand may have
        replaced it (newer bytes the old publish must not outrank). Returns
        whether the discard happened; False tells the caller its publish is a
        resurrect to take back.
        """
        with self._lock:
            if self._entries.get(key) is entry:
                self._discard_locked(key)
                return True
            return False

    def _discard_locked(self, key: str) -> None:
        existing = self._entries.pop(key, None)
        if isinstance(existing, _MemoryEntry):
            self._bytes -= len(existing.image.data)


class ArtistImageCache:
    def __init__(self, cache_dir: Path | str) -> None:
        self._dir = Path(cache_dir)
        self._memory = _MemoryFallback(
            max_entries=_FALLBACK_MAX_ENTRIES, max_bytes=_FALLBACK_MAX_BYTES
        )

    def _key(self, name: str) -> str:
        normalized = normalize_artist_name(name)
        return hashlib.sha1(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()

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
            self._dir / f"{key}{_OVERRIDE_SUFFIX}", self._dir / f"{key}{_OVERRIDE_MIME_SUFFIX}"
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
            warn_throttled("cache-read", _CACHE_DIR_UNREADABLE, exc)

        # Disk had nothing usable. Consult the memory tier LAST so a healthy
        # disk always wins: it only ever holds what a failed write could not
        # store.
        return self._from_memory(key)

    def _from_memory(self, key: str) -> CachedImage | _Negative | None:
        """The memory tier's answer for ``key``, plus the lazy write-back.

        Reached only once disk has answered "nothing usable", so this is the
        one place that knows a strand was actually TOUCHED — which is exactly
        when the retry is worth paying for. ``store_positive`` cannot do it:
        it runs only after ``get()`` MISSES, and a strand makes ``get()`` hit,
        so on the stranded keys it is never called again.

        Negatives are deliberately excluded from the write-back: they carry
        their own expiry and self-heal on TTL, and materialising a ``.miss``
        from a mere probe would bar the re-resolve that repairs the key.
        """
        remembered = self._memory.get(key)
        if isinstance(remembered, _MemoryEntry):
            self._write_back(key, remembered)
            return remembered.image
        if isinstance(remembered, _NegativeUntil):
            if time.time() < remembered.expiry:
                return NEGATIVE
            self._memory.discard(key)
        return None

    def _write_back(self, key: str, entry: _MemoryEntry) -> None:
        """Re-attempt the disk write that stranded ``entry``; on success let go.

        Success hands authority back to disk exactly as ``_or_remember``'s
        success branch does — the entry is DROPPED, not merely out-voted. The
        publish runs outside the map's lock, so the drop is an identity
        compare-and-discard: if the strand vanished mid-publish (a concurrent
        ``clear_auto`` — the reset must stay reset) or was replaced by a fresh
        strand (newer bytes), the files just published are a resurrect and are
        taken back, best effort. A racing ``store_positive`` from a concurrent
        resolve can still re-land legitimately after the take-back; the reset
        route's zero-grace refill covers that the same way it covers its own
        sweep race.

        Failure is swallowed AND leaves the map untouched: no re-put. Re-putting
        would refresh the entry's eviction position on every read, so a broken
        dir plus a busy roster would keep the newest-touched entries and evict
        the ones nothing happened to look at — turning a bounded oldest-first
        map into an access-ordered one nobody asked for.
        """
        try:
            self._publish_positive(key, entry.image)
        except OSError as exc:
            warn_throttled("cache-write", _CACHE_DIR_UNWRITABLE, exc)
            return
        if self._memory.discard_if(key, entry):
            return
        try:
            for suffix in (_BIN_SUFFIX, _MIME_SUFFIX):
                (self._dir / f"{key}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass

    def has_fresh_negative(self, name: str) -> bool:
        """Whether an unexpired no-match marker bars a re-fetch of ``name``.

        The BYTE-FREE half of ``get()``: the caller has already learned from
        ``validator()`` that no image slot exists, and only needs to know
        whether the negative marker is still inside its TTL. Reading the ~20
        byte ``.miss`` body instead of re-running ``get()`` keeps a cold-page
        miss from re-reading (and discarding) up to 10 MB per request.

        Unlike ``get()`` this does NOT unlink a stale marker — it is a probe,
        and the resolve that follows supersedes the marker anyway. Guarded like
        every other read here: an unreadable cache dir reads as "no marker", so
        the caller falls through to its resolve path rather than 500ing.
        """
        key = self._key(name)
        miss = self._dir / f"{key}.miss"
        try:
            if miss.exists() and time.time() < self._read_expiry(miss):
                return True
        except OSError as exc:
            warn_throttled("cache-read", _CACHE_DIR_UNREADABLE, exc)
        remembered = self._memory.get(key)
        return isinstance(remembered, _NegativeUntil) and time.time() < remembered.expiry

    def store_positive(self, name: str, data: bytes, content_type: str) -> None:
        key = self._key(name)
        image = CachedImage(data=data, content_type=content_type)
        # A cache write may never fail the request that triggered it: the caller
        # already HAS the image and is about to serve it, so an unwritable cache
        # dir (volume re-chowned on recreate, read-only mount, disk full) must
        # cost the caching, not the response.
        with self._or_remember(key, image):
            self._publish_positive(key, image)

    def _publish_positive(self, key: str, image: CachedImage) -> None:
        """Publish the positive slot for ``key``. RAISES on a refusing dir.

        Shared with the lazy write-back (:meth:`_write_back`) so a strand
        re-persists through the exact same sequence that stored it originally —
        a second copy of this would be free to drift on the ``.miss`` unlink or
        the sidecar ordering, both of which are load-bearing.

        The one method here that does NOT swallow ``OSError``: its two callers
        need to know whether disk took the entry (one to remember it, one to
        forget it), so the guard is theirs, not this one's.
        """
        self._ensure_dir()
        # A positive result supersedes any prior negative marker.
        (self._dir / f"{key}.miss").unlink(missing_ok=True)
        # Write the mime sidecar BEFORE the bytes so a concurrent get() never
        # reads image bytes paired with a missing/stale content-type. Both writes
        # are atomic (tmp + os.replace) so a reader never catches a truncated
        # file and a crash can't leave a corrupt image cached (see _read_image).
        _atomic_write_bytes(self._dir / f"{key}.mime", image.content_type.encode("utf-8"))
        _atomic_write_bytes(self._dir / f"{key}.bin", image.data)

    def store_negative(self, name: str, *, ttl_seconds: float) -> None:
        key = self._key(name)
        # The marker is the ONLY brake on re-fetching a confirmed no-match, so
        # losing it is the expensive failure here, not the cheap one.
        expiry = time.time() + ttl_seconds
        with self._or_remember(key, _NegativeUntil(expiry=expiry)):
            self._ensure_dir()
            # Store an absolute expiry; the TTL travels with the marker.
            (self._dir / f"{key}.miss").write_text(repr(expiry), encoding="utf-8")

    @contextmanager
    def _or_remember(self, key: str, value: CachedImage | _NegativeUntil) -> Iterator[None]:
        """Run a cache write; on OSError keep ``value`` in the memory tier instead.

        Never raises — see the module docstring for why the tier exists rather
        than a bare swallow. On SUCCESS any earlier stand-in for this key is
        dropped, so a cache dir that gets fixed hands authority back to disk
        without a restart.

        This branch is no longer the ONLY way that happens. It fires when a key
        is written again, which post-repair the stranded keys never are (a
        strand makes ``get()`` hit, so ``store_positive`` is never reached for
        it) — the lazy write-back in :meth:`_write_back` is what covers them,
        with the same drop-on-success semantics.
        """
        try:
            yield
        except OSError as exc:
            self._memory.put(key, value)
            warn_throttled("cache-write", _CACHE_DIR_UNWRITABLE, exc)
        else:
            self._memory.discard(key)

    def write_override(self, name: str, data: bytes, content_type: str) -> None:
        """Plant a manual override (always wins). No auto-writer in this chunk."""
        self._ensure_dir()
        key = self._key(name)
        # Mime before bytes, both atomic (see store_positive).
        _atomic_write_bytes(
            self._dir / f"{key}{_OVERRIDE_MIME_SUFFIX}", content_type.encode("utf-8")
        )
        _atomic_write_bytes(self._dir / f"{key}{_OVERRIDE_SUFFIX}", data)

    def _unlink(self, path: Path) -> bool:
        """Remove one slot file. True when it was there and is now gone.

        Never raises: ``unlink(missing_ok=True)`` still propagates EACCES (a
        container recreate that re-chowned the cache volume) and EROFS, and the
        reset endpoints' whole contract is to REPORT what they did rather than
        500. A refusal reads as "nothing cleared", which is the truth.

        Throttled by CONDITION, not by path: a broken cache dir refuses all five
        of ``clear_auto``'s slots, so an unthrottled record would turn one fact
        into five identical lines per reset.
        """
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            warn_throttled(
                "cache-write",
                "artist-image cache slot %s could not be removed: %s",
                path.name,
                exc,
            )
            return False
        return True

    def _clear_slots(self, image: Path, *sidecars: Path) -> bool:
        """Remove an image slot and its sidecars; report ONLY the image slot.

        The image path is a separate parameter rather than the first of a list
        so the answer cannot drift onto a sidecar: the sidecar results are
        discarded at the language level, not by convention. That matters because
        the divergence is reachable — ``store_positive`` and ``write_override``
        both publish the mime BEFORE the bytes, so a crash between the two
        leaves an orphaned sidecar, and a clear that answered "something went
        away" would report a reset that never happened.

        Image before sidecars, so a concurrent ``get()`` never pairs image bytes
        with a vanished mime.
        """
        removed = self._unlink(image)
        for sidecar in sidecars:
            self._unlink(sidecar)
        return removed

    def clear_override(self, name: str) -> bool:
        """Remove a manual override -> next get() falls back to auto/cache.

        Unlink the BYTES before the MIME (mirror-image of write_override's
        mime-before-bytes order) so a concurrent get() never reads override
        bytes paired with a missing mime sidecar. Returns whether an override
        was actually removed — the ``.override`` slot's outcome, never the
        sidecar's (see :meth:`_clear_slots`).

        Scoped to the override slot ONLY, which is why it cannot be the whole
        of a "reset to automatic": the ``.bin`` it falls back to is the image
        the user just rejected. See :meth:`clear_auto`.

        No in-memory drop here on purpose: ``write_override`` does not go
        through ``_or_remember``, so an override never reaches
        :class:`_MemoryFallback` and there is nothing of its own to forget.
        """
        key = self._key(name)
        return self._clear_slots(
            self._dir / f"{key}{_OVERRIDE_SUFFIX}", self._dir / f"{key}{_OVERRIDE_MIME_SUFFIX}"
        )

    def clear_auto(self, name: str) -> bool:
        """Forget the AUTOMATIC image for ``name`` so the next lookup re-resolves.

        Removes the positive slot, its mime sidecar, the negative marker and the
        derived thumb, and drops any in-memory fallback entry for the key.
        Returns whether the POSITIVE SLOT was actually removed — the mime, the
        marker and the thumb are housekeeping, swept but never reported, which
        :meth:`_clear_slots` enforces by construction.

        Nothing else in this module unlinks ``.bin``: ``store_positive`` only
        runs on a cache MISS, and a present ``.bin`` means there is never a
        miss, so without this the automatic image could never change once
        written. The in-memory drop is not optional -- on a cache dir that
        refused the write, the image (or the negative marker) lives in
        :class:`_MemoryFallback`, and unlinking files alone would keep serving
        it for the life of the process.

        Bytes before mime (mirroring :meth:`clear_override`) so a concurrent
        ``get()`` never pairs image bytes with a vanished sidecar. The thumb
        pair needs no ordering: either file missing is a thumb-cache miss, which
        re-derives.
        """
        key = self._key(name)
        removed = self._clear_slots(
            self._dir / f"{key}.bin",
            self._dir / f"{key}.mime",
            self._dir / f"{key}.miss",
            self._dir / f"{key}.thumb.bin",
            self._dir / f"{key}.thumb.src",
        )
        self._memory.discard(key)
        return removed

    def rename(self, old_name: str, new_name: str) -> Literal["moved", "kept_target", "none"]:
        """Re-key every slot from ``old_name`` to ``new_name`` after an artist rename.

        Called ONLY when the old name has ceased to exist (its last album
        renamed away), so nothing may remain under the old key afterwards.

        * Same normalized key (case/accent-only rename): nothing to do —
          "moved" when a portrait exists (it already serves the new name),
          else "none".
        * The target already has a REAL portrait (override or positive, disk
          or memory): it wins, with one precedence rule — a MANUAL PIN on the
          source outranks the target's bare auto image. So: target pin wins
          outright ("kept_target"); target auto image wins when the source
          has no pin ("kept_target"); a source pin over a target auto image
          moves the override pair to the new key — reported "moved" when the
          pin actually landed, "kept_target" when the move failed (the
          target's auto image is then still what get() serves) — and the
          target's auto slot is left beneath it (it resurfaces if the user
          later clears the pin). Every OTHER old-key file is deleted. Leaving
          old-key files would recreate the forever-orphan this method exists
          to prevent.
        * Otherwise the old key's image slots move to the new key. A negative
          ``.miss`` never travels in either direction: it recorded "sources
          had no image for the OLD name", and the new name deserves a fresh
          lookup — likewise a stale ``.miss`` on the target must not outvote
          the real portrait arriving.

        Never raises (the reset-endpoint contract): every file op is guarded,
        and a failed move leaves that file under the old key — degraded to
        today's orphan behaviour, never a 500. Sidecar-before-bytes ordering
        mirrors the writers, so a concurrent ``get`` never pairs image bytes
        with a stale mime.
        """
        old_key, new_key = self._key(old_name), self._key(new_name)
        if old_key == new_key:
            return "moved" if self._has_portrait(old_key) else "none"
        if self._has_override(new_key):
            self._purge_old_key(old_key)
            return "kept_target"
        if self._has_portrait(new_key) and not self._has_override(old_key):
            self._purge_old_key(old_key)
            return "kept_target"
        if self._has_portrait(new_key):
            moved_pin = self._rename_pin_onto_auto(old_key, new_key)
            return "moved" if moved_pin else "kept_target"
        return "moved" if self._rename_move_all(old_key, new_key) else "none"

    def _purge_old_key(self, old_key: str) -> None:
        """Delete every old-key slot file and the memory fallback entry.

        Used by the target-wins branches: the target's portrait stays, but the
        old artist is gone, so nothing may remain under the old key — leaving
        old-key files would recreate the forever-orphan :meth:`rename` exists
        to prevent.
        """
        for suffix in _ALL_SLOT_SUFFIXES:
            self._unlink(self._dir / f"{old_key}{suffix}")
        self._memory.discard(old_key)

    def _rename_pin_onto_auto(self, old_key: str, new_key: str) -> bool:
        """Move a source MANUAL PIN onto the target's auto image.

        A source manual pin outranks the target's auto image: move only the
        override pair (sidecar before bytes — that order is load-bearing for a
        concurrent ``get()``), delete the rest of the old key (the old artist
        is gone), and leave the target's auto slot beneath the pin — it
        resurfaces if the user clears it. Returns whether the pin BYTES
        actually relocated.
        """
        self._replace(
            self._dir / f"{old_key}{_OVERRIDE_MIME_SUFFIX}",
            self._dir / f"{new_key}{_OVERRIDE_MIME_SUFFIX}",
        )
        moved_pin = self._replace(
            self._dir / f"{old_key}{_OVERRIDE_SUFFIX}", self._dir / f"{new_key}{_OVERRIDE_SUFFIX}"
        )
        for suffix in _ALL_SLOT_SUFFIXES:
            if suffix not in (_OVERRIDE_SUFFIX, _OVERRIDE_MIME_SUFFIX):
                self._unlink(self._dir / f"{old_key}{suffix}")
        self._unlink(self._dir / f"{new_key}.miss")
        self._memory.discard(old_key)
        return moved_pin

    def _rename_move_all(self, old_key: str, new_key: str) -> bool:
        """Move every old-key slot to the new key; True iff anything moved.

        A negative ``.miss`` never travels in either direction: it recorded
        "sources had no image for the OLD name", and the new name deserves a
        fresh lookup — likewise a stale ``.miss`` on the target must not
        outvote the real portrait arriving.

        Slots move in :data:`_MOVE_ORDER` exactly: its ordering encodes
        sidecar-before-bytes, mirroring the writers, so a concurrent ``get``
        never pairs image bytes with a stale mime. A memory-fallback
        :class:`CachedImage` re-put under the new key also counts as moved;
        the old key is always discarded from memory.
        """
        self._unlink(self._dir / f"{old_key}.miss")
        self._unlink(self._dir / f"{new_key}.miss")
        moved = False
        for suffix in _MOVE_ORDER:
            relocated = self._replace(
                self._dir / f"{old_key}{suffix}", self._dir / f"{new_key}{suffix}"
            )
            if suffix in (_OVERRIDE_SUFFIX, _BIN_SUFFIX):
                moved = moved or relocated
        remembered = self._memory.get(old_key)
        if isinstance(remembered, _MemoryEntry):
            self._memory.put(new_key, remembered)
            moved = True
        self._memory.discard(old_key)
        return moved

    def _has_override(self, key: str) -> bool:
        """Whether a MANUAL PIN (``.override``) exists under ``key``.

        Guarded like ``_has_portrait``: an unreadable cache dir reads as "no
        pin". Nothing to check in the memory fallback — ``write_override``
        does not go through ``_or_remember``, so a pin never lands there.
        """
        try:
            return (self._dir / f"{key}{_OVERRIDE_SUFFIX}").exists()
        except OSError as exc:
            warn_throttled("cache-read", _CACHE_DIR_UNREADABLE, exc)
            return False

    def _has_portrait(self, key: str) -> bool:
        """Whether a REAL image (override or positive) exists under ``key`` —
        on disk or stranded in the memory fallback. Guarded: an unreadable
        cache dir reads as "no portrait" (same posture as ``validator``)."""
        try:
            for suffix in (_OVERRIDE_SUFFIX, _BIN_SUFFIX):
                if (self._dir / f"{key}{suffix}").exists():
                    return True
        except OSError as exc:
            warn_throttled("cache-read", _CACHE_DIR_UNREADABLE, exc)
        return isinstance(self._memory.get(key), _MemoryEntry)

    def _replace(self, old: Path, new: Path) -> bool:
        """Move one slot file. True when it was there and is now at ``new``.
        Never raises — same contract and throttle keys as ``_unlink``."""
        try:
            os.replace(old, new)
        except FileNotFoundError:
            return False
        except OSError as exc:
            warn_throttled(
                "cache-write",
                "artist-image cache slot %s could not be renamed: %s",
                old.name,
                exc,
            )
            return False
        return True

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
          straight off a CDN (see ``header_safe_content_type``), so it is a
          poisoned-slot case, not a corrupted-disk one.

        The ``exists()`` calls are guarded rather than trusted: ``Path.exists()``
        only swallows ENOENT/ENOTDIR/EBADF/ELOOP, so EACCES (a container
        recreate that re-chowned the cache volume) or ESTALE (a dropped network
        mount) would otherwise raise from inside a function that promises not
        to — same shape as :func:`app.etag.stat_etag`.
        """
        try:
            if not data_path.exists():
                return None
            data = data_path.read_bytes()
        except OSError as exc:
            # exists()-then-read is also a window the backfill daemon's atomic
            # replace — and the clear_auto / clear_override unlinks — can move
            # under us.
            warn_throttled("cache-read", "artist-image cache slot is unreadable: %s", exc)
            return None
        content_type = FALLBACK_CONTENT_TYPE
        try:
            if mime_path.exists():
                content_type = mime_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError) as exc:
            warn_throttled(
                "mime-sidecar-unreadable",
                "artist-image mime sidecar %s is unreadable, serving the image as %s: %s",
                mime_path.name,
                FALLBACK_CONTENT_TYPE,
                exc,
            )
            content_type = FALLBACK_CONTENT_TYPE
        sendable = header_safe_content_type(content_type)
        if sendable is None:
            # Logged like every other degrade now that the throttle exists: this
            # is a READ of stored state that nothing rewrites, so an unthrottled
            # line here repeats on every request for as long as the slot stays
            # poisoned. Keyed by condition, it reports once and stays reported.
            warn_throttled(
                "mime-sidecar-unsendable",
                "artist-image mime sidecar %s holds a content-type that cannot be "
                "sent (%r); serving %s",
                mime_path.name,
                content_type,
                FALLBACK_CONTENT_TYPE,
            )
        # The RETURN value: a sidecar that only needed trimming is served
        # trimmed. The .strip() above already handles the sidecar's own body,
        # but this is the value that reaches the header, so it takes the rule.
        return CachedImage(data=data, content_type=sendable or FALLBACK_CONTENT_TYPE)

    def validator(self, name: str) -> str | None:
        """Cheap revalidation tag for the image get() would serve: the override
        slot when present, else the positive slot, else the MEMORY tier's own
        tag. None when none of the three exists (miss or negative — the caller
        falls through to the resolve path). Same stat-based scheme as the album
        cover's cover_validator for the disk slots: any writer replaces the file
        (atomic rename bumps mtime), so a stale tag can never yield a false 304.

        Disk is probed FIRST and the memory tier only answers for what disk does
        not have, which is the same disk-then-memory order ``get()`` and
        ``_has_portrait`` use — the three must agree or a tag would validate an
        image the endpoint is not about to serve. Skipping the tier here was the
        recorded bug: a stranded image looked unvalidatable, so every request for
        it bypassed the 304 path into the background filler, whose completed fill
        armed an unscoped ``art:changed`` and fed the next request — a
        self-sustaining remount loop, on top of a per-request sha256 and thumb
        re-derive. A memory tag cannot collide with a stat tag (see
        :data:`_MEMORY_TAG_MARK`).

        The probe is guarded for the same reason :func:`app.etag.stat_etag` is:
        this runs FIRST on every image request, so an unreadable cache dir (EACCES after a
        volume re-chown, ESTALE on a dropped mount) raising out of ``exists()``
        here would 500 both endpoints before any other guard could catch it.
        Unreadable reads as "nothing to validate" — the caller falls through to
        its resolve path exactly as it does on cache miss. The ``stat`` itself
        also races the backfill daemon's atomic replace, so a vanished file makes
        the tag ``None`` and the caller serves the full body with a content-hash
        fallback rather than trusting a stale tag."""
        key = self._key(name)
        try:
            for slot in (f"{key}{_OVERRIDE_SUFFIX}", f"{key}.bin"):
                path = self._dir / slot
                if path.exists():
                    return stat_etag(path)
        except OSError as exc:
            warn_throttled(
                "cache-read", "artist-image cache dir is unreadable (%s); serving unvalidated", exc
            )
        remembered = self._memory.get(key)
        if not isinstance(remembered, _MemoryEntry):
            return None
        # get() ranks a FRESH disk negative above the memory tier, and the tag
        # must rank it the same way or it validates an image get() refuses to
        # serve. In-process the pair cannot arise (a strand blocks the resolve
        # that writes markers), but the backfill daemon's separate cache
        # instance over the same dir can write one while this instance holds a
        # strand — and then a client holding the memory tag would keep 304ing
        # the stale portrait while everyone else 404s. An unreadable dir makes
        # no marker knowable, which is also what get()'s guarded probe
        # concludes before serving the strand — the tag stays honest.
        try:
            miss = self._dir / f"{key}.miss"
            if miss.exists() and time.time() < self._read_expiry(miss):
                return None
        except OSError:
            pass
        return remembered.tag

    def get_thumb(self, name: str) -> CachedImage | None:
        """The 320px WebP derivation of what get() would serve, deriving (and
        caching) it if the stored one is missing or was built from different
        source bytes. None when no source image is cached (the caller resolves
        first, then retries). An undecodable source degrades to the original
        bytes — a grid that shows SOME image beats a 500.

        A source that lives in the MEMORY tier is derived and SERVED but never
        cached: its tag dies with the process, so a ``.thumb`` pair written under
        it would outlive every reader that could match it — a permanent orphan no
        sweep looks for, since ``clear_auto`` only touches keys someone resets.
        That costs a derive per request while the strand lasts, which is exactly
        what the strand cost before; the very next call caches normally, because
        ``self.get`` below has by then written the source back to disk and the
        following request's validator is a stat tag. Deliberately NOT re-running
        ``validator()`` mid-flow to pick that tag up: the tag this response is
        served under is the one the caller already put on the wire."""
        src_tag = self.validator(name)
        if src_tag is None:
            return None
        key = self._key(name)
        thumb_path = self._dir / f"{key}.thumb.bin"
        src_path = self._dir / f"{key}.thumb.src"
        try:
            stored = src_path.read_text(encoding="utf-8")
            stored_tag, _, stored_mime = stored.partition(" ")
            # A stored mime that cannot BE a header (non-ASCII, a control
            # character) misses on purpose rather than falling back: re-deriving
            # replaces it with this cache's own THUMB_MIME, which is strictly
            # better than serving a WebP labelled application/octet-stream. One
            # that merely needs trimming HITS, and is served trimmed — the
            # sendable value, never the stored one.
            sendable = header_safe_content_type(stored_mime)
            if stored_tag == src_tag and sendable is not None and thumb_path.exists():
                return CachedImage(data=thumb_path.read_bytes(), content_type=sendable)
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
        # Shared with CoverThumbCache and with the endpoint's uncached path, so
        # the serve-or-degrade decision — and the record it leaves — is made in
        # exactly one place. It also re-checks the mime: _read_image already
        # guarantees a header-safe, non-empty type, but get_thumb does not own
        # that invariant and this value goes on the wire.
        data, mime = derive_thumb_or_degrade(
            source.data, source.content_type, subject=f"artist {name!r}"
        )
        # Caching is best-effort: the thumb is already derived, so an unwritable
        # cache dir must cost the caching, not the image. A memory-tier source
        # is not cached at all (see the docstring) — the pair would be keyed to a
        # tag no restart can reproduce.
        if not _is_memory_tag(src_tag):
            try:
                self._ensure_dir()
                _atomic_write_bytes(thumb_path, data)
                _atomic_write_bytes(src_path, f"{src_tag} {mime}".encode())
            except OSError as exc:
                warn_throttled("cache-write", "artist-image thumb cache is unwritable: %s", exc)
        return CachedImage(data=data, content_type=mime)
