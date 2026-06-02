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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import beets
import confuse
from beets.library import Library
from beets.ui import should_write
from beetsplug._utils import art
from fastapi import Request

from app.models.cover import CoverInstallResult

_log = logging.getLogger("musicdrop.cover")

# Hard cap on cover bytes we will materialize, mirroring the API upload cap. A
# remote fetchart candidate can point at an arbitrarily large download; refuse to
# read it into memory past this size (DoS guard).
MAX_COVER_BYTES = 10 * 1024 * 1024

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


def install_cover(lib: Library, *, album_id: int, image_bytes: bytes) -> CoverInstallResult:
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


@dataclass
class FetchedCover:
    """A cover candidate read into memory (bytes never cross the API as JSON)."""

    image_bytes: bytes
    content_type: str
    source: str


def _make_fetchart_plugin() -> Any:
    """A throwaway FetchArtPlugin that will NOT register import hooks.

    ``FetchArtPlugin.__init__`` wires import_stages + a listener iff
    ``config['fetchart']['auto']`` is truthy (default True). Forcing it False
    first keeps the user's import auto-fetch behavior unchanged.
    """
    from beetsplug.fetchart import FetchArtPlugin

    beets.config["fetchart"].set({"auto": False})
    return FetchArtPlugin()


def fetch_cover_candidate(lib: Library, *, album_id: int) -> FetchedCover | None:
    """Fetch beets' best cover candidate for an album. Writes nothing to the library."""
    with lib.music_dir_context():
        album = lib.get_album(album_id)
        if album is None:
            raise AlbumNotFoundError(f"album {album_id} not found")
        plugin = _make_fetchart_plugin()
        candidate = plugin.art_for_album(album, [album.path], local_only=False)
        if candidate is None:
            return None
        path = os.fsdecode(candidate.path)
        source = str(getattr(candidate, "source_name", None) or "external source")
        # Remote temp downloads get cleaned up; the album's own folder art does not.
        libdir = os.path.abspath(os.fsdecode(lib.directory))
        is_remote = not os.path.abspath(path).startswith(libdir + os.sep)

        def _cleanup() -> None:
            if is_remote:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        # Cap before reading so a huge remote image is never materialized (DoS).
        if os.path.getsize(path) > MAX_COVER_BYTES:
            _cleanup()
            return None
        data = Path(path).read_bytes()
        _cleanup()
    mime = _sniff_mime(data)
    if mime is None:
        return None  # candidate wasn't a recognizable image -> treat as not found
    return FetchedCover(image_bytes=data, content_type=mime, source=source)


async def fetch_cover_op(request_obj: Request, album_id: int) -> tuple[bytes, str, str]:
    """Fetch a candidate (no library write -> no lock/gate). Returns (bytes, mime, source)."""
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool

    handle = request_obj.app.state.beets_library
    try:
        fetched = await run_in_threadpool(fetch_cover_candidate, handle.lib, album_id=album_id)
    except AlbumNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if fetched is None:
        raise HTTPException(status_code=404, detail="No cover found")
    return fetched.image_bytes, fetched.content_type, fetched.source


async def install_cover_op(
    request_obj: Request, album_id: int, image_bytes: bytes
) -> CoverInstallResult:
    """Install: import-gate (409) + shared swap-lock + threadpool, like edit/duplicates."""
    from fastapi import HTTPException
    from fastapi.concurrency import run_in_threadpool

    from app.beets.config_editor import _swap_lock
    from app.import_jobs.registry import get_registry

    app = request_obj.app
    if get_registry().has_active_job():
        raise HTTPException(
            status_code=409, detail="Import in progress — cover changes available when it finishes"
        )
    async with _swap_lock(app):
        handle = app.state.beets_library
        try:
            return await run_in_threadpool(
                install_cover,
                handle.lib,
                album_id=album_id,
                image_bytes=image_bytes,
            )
        except AlbumNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (EmptyAlbumError, UnsupportedImageError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # structured 500 like config Apply / edit
            raise HTTPException(
                status_code=500,
                detail={"message": f"Cover install failed: {exc}", "recovery": "Reload and retry."},
            ) from exc
