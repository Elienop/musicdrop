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

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import beets
import confuse
from beets import logging as beets_logging
from beets.library import Library
from beets.ui import should_write
from beetsplug._utils import art
from fastapi import Request

from app.artwork.images import MAX_IMAGE_BYTES, sniff_image_mime
from app.models.cover import CoverInstallResult

# The logger the embedart plugin hands ``embed_album`` (its plugin logger,
# ``getLogger("beets").getChild("embedart")``): a BeetsLogger, which formats
# beets' ``{}``-style messages. A stdlib logger fails on them ("--- Logging error ---").
_log = beets_logging.getLogger("beets.embedart")

# Re-exported from the shared image utils so cover + artist uploads share one cap.
MAX_COVER_BYTES = MAX_IMAGE_BYTES

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
    """Album has no items, so beets cannot compute an art destination.

    Maps to 422: the image is fine, the target cannot take it.
    """


class UnsupportedImageError(Exception):
    """The bytes are not a supported image (png/jpeg/gif/webp).

    Maps to 415, matching every other image upload in this API.
    """


def _embed_enabled() -> bool:
    """Config gate: the user opted ``embedart`` into their plugins AND writes are on."""
    plugins = beets.config["plugins"].as_str_seq()
    return "embedart" in plugins and bool(should_write(None))


def install_cover(lib: Library, *, album_id: int, image_bytes: bytes) -> CoverInstallResult:
    """Install ``image_bytes`` as the album cover (artpath) + optional embed."""
    mime = sniff_image_mime(image_bytes)
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
                # set_art takes a bytes path (it fsencodes internally anyway).
                album.set_art(os.fsencode(tmp))  # copies -> <album dir>/cover.<ext>, sets artpath
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

    The False overlay is RESTORED after construction so this fetch doesn't leave
    a persistent global-config mutation (``fetchart.auto`` reading False for
    everything after). We restore the user's explicit value if they set one, else
    the plugin default (True). NOTE: this does not serialize against a concurrent
    config Apply replacing ``beets.config``'s sources — that rare single-user timing race is
    a documented residual; removing the persistent overlay is the fix here.
    """
    from beetsplug.fetchart import FetchArtPlugin

    fetchart = beets.config["fetchart"]
    try:
        prev_auto: bool | None = fetchart["auto"].get(bool)
    except confuse.NotFoundError:
        prev_auto = None  # unset before construction — restore to the default
    fetchart.set({"auto": False})
    try:
        return FetchArtPlugin()
    finally:
        fetchart.set({"auto": True if prev_auto is None else prev_auto})


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
    mime = sniff_image_mime(data)
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
    from app.library_busy import library_job_active, raise_if_swap_blocked_by_job

    app = request_obj.app
    busy = "A library operation is in progress; cover changes available when it finishes"
    if library_job_active():
        raise HTTPException(status_code=409, detail=busy)
    async with _swap_lock(app):
        # Asked again, now the lock is ours: see ``library_busy``'s swap-lock note.
        raise_if_swap_blocked_by_job(message=busy)
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
        except UnsupportedImageError as exc:
            # Unsupported MEDIA TYPE, like the artist-portrait and playlist-artwork
            # uploads. Distinct from EmptyAlbumError below, which is a supported
            # image the album simply cannot accept.
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except EmptyAlbumError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # structured 500 like config Apply / edit
            raise HTTPException(
                status_code=500,
                detail={"message": f"Cover install failed: {exc}", "recovery": "Reload and retry."},
            ) from exc
