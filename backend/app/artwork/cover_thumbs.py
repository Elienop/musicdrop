"""On-disk derivation cache for album-cover thumbnails.

Layout (under the configured cache dir), keyed by album id::

    <album_id>.bin   derived 320px WebP (or, degraded, a verbatim copy of the
                     source bytes — see get)
    <album_id>.src   "<source validator> <content-type of .bin>"

Unlike :class:`~app.artwork.cache.ArtistImageCache` (which also owns the
original image), this cache only ever holds the DERIVED thumb — the original
cover lives in the beets library (artpath file or embedded tag) and is handed
in on demand via ``load_original``. The ``.src`` sidecar's validator is the
caller's cheap stat-based tag for that original (``cover_validator``), so a
changed source (new art fetched, embedded art re-tagged) is detected without
reading either file's bytes first.

Pure filesystem; no network, no beets import (kept out of app/beets/ per the
project's adapter boundary since this module never touches ``import beets``).

Rebuildable derivation cache — safe to delete entirely; the next request just
re-derives. Single-writer-per-file is NOT assumed: two concurrent requests can
both miss and both derive, and both write atomically — last writer wins, and
since both derive from the same source bytes the result is the same either way.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.artwork.cache import CachedImage, _atomic_write_bytes
from app.artwork.thumbs import THUMB_MIME, ThumbError, make_thumb

_log = logging.getLogger("musicdrop.artwork")


class CoverThumbCache:
    def __init__(self, cache_dir: Path | str) -> None:
        self._dir = Path(cache_dir)

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(
        self,
        album_id: int,
        source_tag: str,
        load_original: Callable[[], tuple[bytes, str] | None],
    ) -> CachedImage | None:
        """The 320px WebP derivation of the cover ``load_original`` would load,
        deriving (and caching) it if the stored thumb is missing or was built
        from different source bytes (``source_tag`` mismatch — the caller's
        cheap stat validator for the original, e.g. ``cover_validator``).

        ``load_original`` is invoked at most once, only on a cache miss, so an
        unchanged cover never re-reads the source file. Returns ``None`` when
        ``load_original`` returns ``None`` (no cover for this album). An
        undecodable source degrades to the original bytes/mime — a grid that
        shows SOME image beats a 500.
        """
        bin_path = self._dir / f"{album_id}.bin"
        src_path = self._dir / f"{album_id}.src"
        try:
            stored = src_path.read_text(encoding="utf-8")
            stored_tag, _, stored_mime = stored.partition(" ")
            if stored_tag == source_tag and stored_mime and bin_path.exists():
                return CachedImage(data=bin_path.read_bytes(), content_type=stored_mime)
        except (OSError, ValueError):
            # Missing/unreadable sidecar — rederive below. ValueError covers
            # UnicodeDecodeError (a non-UTF-8 body): this cache is safe to
            # delete entirely, so anything unreadable simply rebuilds rather
            # than 500ing the cover endpoint.
            pass

        original = load_original()
        if original is None:
            return None
        data, mime = original
        try:
            thumb_data = make_thumb(data)
            thumb_mime = THUMB_MIME
        except ThumbError as exc:
            # Logged for the same reason as ArtistImageCache.get_thumb's degrade
            # (see there): a systemic encoder failure used to be a 500, and
            # silent full-size fallback looks exactly like an undeployed
            # feature. Once per source version — the fallback is cached below.
            _log.warning(
                "album-cover thumbnail degraded to the original for album %s: %s", album_id, exc
            )
            thumb_data, thumb_mime = data, mime or "application/octet-stream"

        self._ensure_dir()
        # .bin BEFORE .src: the .src tag is what makes an entry "hit" on the
        # next get(), so a crash between the two writes must leave the tag
        # unwritten/stale — forcing a re-derive next time — rather than leaving
        # a fresh tag pointing at stale (previous-source) bytes in .bin.
        _atomic_write_bytes(bin_path, thumb_data)
        _atomic_write_bytes(src_path, f"{source_tag} {thumb_mime}".encode())
        return CachedImage(data=thumb_data, content_type=thumb_mime)
