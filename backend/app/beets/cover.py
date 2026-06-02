"""Album cover-art adapter — fetch + install, on the write boundary.

All beets cover access lives here (CLAUDE.md rule 3). Install mirrors beets:
``album.set_art(path)`` copies the image to ``<album dir>/cover.<ext>`` and sets
``artpath`` (it does NOT persist — we call ``album.store()``); embedding is
explicit and config-gated via ``art.embed_album`` (our config doesn't load
``embedart``, so set_art's ``art_set`` event embeds nothing — see memory
``beets-cover-art-contract``). Off-main-thread work binds
``lib.music_dir_context()`` because ``artpath`` is DB-relative + ContextVar-
expanded (same root cause as PR #15).
"""

from __future__ import annotations

import logging
import os
import tempfile

import beets
import confuse
from beets.library import Library
from beets.ui import should_write
from beetsplug._utils import art

from app.models.cover import CoverInstallResult

_log = logging.getLogger("musicdrop.cover")

# Sniffed mime -> the extension we give the temp file (drives ``cover.<ext>``).
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
# beets only embeds these into media files (beetsplug/_utils/art.py).
_EMBEDDABLE = {"image/jpeg", "image/png"}


class AlbumNotFoundError(Exception):
    """A referenced album id is not in the library. Maps to 404."""


class EmptyAlbumError(Exception):
    """Album has no items, so beets cannot compute an art destination. Maps to 422."""


class UnsupportedImageError(Exception):
    """The bytes are not a supported image (png/jpeg/gif/webp). Maps to 422."""


def _sniff_mime(data: bytes) -> str | None:
    """Best-effort image mime from magic bytes; None if not a known image."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _embed_enabled() -> bool:
    """Config gate: the user opted ``embedart`` into their plugins AND writes are on."""
    plugins = beets.config["plugins"].as_str_seq()
    return "embedart" in plugins and bool(should_write(None))


def install_cover(
    lib: Library, *, album_id: int, image_bytes: bytes, content_type: str | None
) -> CoverInstallResult:
    """Install ``image_bytes`` as the album cover (artpath) + optional embed."""
    mime = _sniff_mime(image_bytes)
    if mime is None:
        raise UnsupportedImageError("not a supported image (png/jpeg/gif/webp)")
    ext = _MIME_EXT[mime]
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        if not list(album.items()):
            raise EmptyAlbumError(f"album {album_id} has no tracks")
        fd, tmp = tempfile.mkstemp(suffix=ext)
        embedded = False
        detail: str | None = "embedart not enabled"
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(image_bytes)
            with lib.transaction():
                album.set_art(tmp)  # copies -> <album dir>/cover.<ext>, sets artpath
                album.store()  # persist artpath
                if _embed_enabled():
                    if mime in _EMBEDDABLE:
                        width = beets.config["embedart"]["maxwidth"].get(confuse.Integer(0))
                        art.embed_album(_log, album, width or None, True)
                        embedded, detail = True, None
                    else:
                        detail = "embedart enabled but cover is not jpeg/png"
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return CoverInstallResult(ok=True, embedded=embedded, embed_detail=detail, message=None)
