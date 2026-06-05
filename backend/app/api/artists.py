from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile

from app.api.albums import get_library
from app.artist_art_jobs.registry import (
    ArtistArtBackfillRegistry,
    artist_art_backfill_active,
    get_artist_art_backfill,
)
from app.artist_art_jobs.runner import start_backfill as start_art_backfill
from app.artwork.cache import ArtistImageCache
from app.artwork.images import MAX_IMAGE_BYTES, sniff_image_mime
from app.artwork.service import ArtistImageService
from app.artwork.toggle import ArtistArtWriteToggle, ArtistImageToggle
from app.beets import library as beets_library
from app.beets.library import LibraryHandle, list_artists
from app.config import settings as _module_settings
from app.import_jobs.registry import get_registry
from app.lyrics_jobs.registry import lyrics_backfill_active
from app.models.artist import Artist, ArtistImageOverrideResult, ArtistImageSettings
from app.models.artist_art import ArtistArtBackfillStatus, ArtistArtWriteSettings
from app.reorganize_jobs.registry import reorganize_backfill_active

router = APIRouter(tags=["artists"])


def get_artist_image_service(request: Request) -> ArtistImageService:
    """Return the process-wide artist-image service built at startup.

    Constructed once in the app lifespan and stored on
    ``app.state.artist_image_service``. Overridden in tests to inject a stub so
    no real Deezer call is made.
    """
    service: ArtistImageService = request.app.state.artist_image_service
    return service


def get_artist_image_toggle(request: Request) -> ArtistImageToggle:
    """The process-wide persisted enabled toggle, built in the app lifespan."""
    toggle: ArtistImageToggle = request.app.state.artist_image_toggle
    return toggle


def get_artist_image_cache(request: Request) -> ArtistImageCache:
    """The process-wide artist-image disk cache, built in the app lifespan."""
    cache: ArtistImageCache = request.app.state.artist_image_cache
    return cache


def get_artist_art_write_toggle(request: Request) -> ArtistArtWriteToggle:
    """The process-wide persisted write-to-library toggle, built in the lifespan."""
    toggle: ArtistArtWriteToggle = request.app.state.artist_art_write_toggle
    return toggle


@router.get("/artists", response_model=list[Artist])
async def list_artists_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> list[Artist]:
    return list_artists(handle.lib)


@router.get(
    "/artists/image",
    responses={
        200: {"content": {"image/*": {}}, "description": "The artist portrait."},
        404: {"description": "Feature disabled, no verified match, or transient error."},
    },
)
async def get_artist_image_endpoint(
    name: Annotated[str, Query(min_length=1)],
    service: Annotated[ArtistImageService, Depends(get_artist_image_service)],
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> Response:
    # Query param (not path) so "AC/DC" works. The MBID is resolved lazily — the
    # service only invokes get_mbid on a cache miss (fanart.tv is MBID-keyed).
    result = await service.get_artist_image(
        name, get_mbid=lambda: beets_library.get_artist_mbid(handle.lib, name)
    )
    if result is None:
        # Covers disabled / no verified match / transient error; the frontend
        # falls back to the person glyph on 404.
        raise HTTPException(status_code=404, detail="Artist image not found")

    image_bytes, mime = result
    return Response(
        content=image_bytes,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/artists/image/settings", response_model=ArtistImageSettings)
async def get_artist_image_settings_endpoint(
    toggle: Annotated[ArtistImageToggle, Depends(get_artist_image_toggle)],
) -> ArtistImageSettings:
    return ArtistImageSettings(enabled=toggle.is_enabled())


@router.put("/artists/image/settings", response_model=ArtistImageSettings)
async def set_artist_image_settings_endpoint(
    body: ArtistImageSettings,
    toggle: Annotated[ArtistImageToggle, Depends(get_artist_image_toggle)],
) -> ArtistImageSettings:
    return ArtistImageSettings(enabled=toggle.set_enabled(body.enabled))


@router.post("/artists/image/override", response_model=ArtistImageOverrideResult)
async def upload_artist_image_override_endpoint(
    request: Request,
    file: UploadFile,
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
) -> ArtistImageOverrideResult:
    # Reject an oversize body before materializing it when the client declares
    # its size; the post-read length check below is the authoritative guard.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=422, detail="Image too large (max 10 MB)")
    image_bytes = await file.read()
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=422, detail="Image too large (max 10 MB)")
    mime = sniff_image_mime(image_bytes)
    if mime is None:
        raise HTTPException(status_code=422, detail="not a supported image (png/jpeg/gif/webp)")
    cache.write_override(name, image_bytes, mime)
    return ArtistImageOverrideResult(ok=True, content_type=mime)


@router.delete("/artists/image/override", status_code=204)
async def clear_artist_image_override_endpoint(
    name: Annotated[str, Query(min_length=1)],
    cache: Annotated[ArtistImageCache, Depends(get_artist_image_cache)],
) -> Response:
    cache.clear_override(name)
    return Response(status_code=204)


def _gate_library_busy(app: object) -> None:
    from fastapi import HTTPException
    from fastapi import status as st

    if (
        get_registry().has_active_job()
        or lyrics_backfill_active()
        or artist_art_backfill_active()
        or reorganize_backfill_active()
    ):
        raise HTTPException(
            st.HTTP_409_CONFLICT,
            "A library operation is in progress — try again when it finishes",
        )
    lock = getattr(app.state, "beets_swap_lock", None)  # type: ignore[attr-defined]
    if lock is not None and lock.locked():
        raise HTTPException(
            st.HTTP_409_CONFLICT,
            "A library operation is in progress — try again when it finishes",
        )


@router.get("/artists/art/settings", response_model=ArtistArtWriteSettings)
async def get_artist_art_settings(
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
) -> ArtistArtWriteSettings:
    return ArtistArtWriteSettings(enabled=toggle.is_enabled())


@router.put("/artists/art/settings", response_model=ArtistArtWriteSettings)
async def set_artist_art_settings(
    body: ArtistArtWriteSettings,
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
) -> ArtistArtWriteSettings:
    return ArtistArtWriteSettings(enabled=toggle.set_enabled(body.enabled))


@router.post("/artists/art/apply", response_model=ArtistArtBackfillStatus)
async def apply_artist_art(
    request: Request,
    name: Annotated[str, Query(min_length=1)],
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    if not toggle.is_enabled():
        raise HTTPException(status_code=403, detail="Enable 'Write artist art to library' first")
    _gate_library_busy(request.app)
    try:
        reg.start(force=True, artist=name, scope_label=name)
    except RuntimeError:
        raise HTTPException(
            status_code=409, detail="An artist-art job is already running"
        ) from None
    handle = request.app.state.beets_library
    _start(request.app, reg, handle.lib, force=True, artist=name)
    return reg.state()


@router.post("/artists/art/backfill", response_model=ArtistArtBackfillStatus)
async def start_artist_art_backfill(
    request: Request,
    toggle: Annotated[ArtistArtWriteToggle, Depends(get_artist_art_write_toggle)],
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    if not toggle.is_enabled():
        raise HTTPException(status_code=403, detail="Enable 'Write artist art to library' first")
    _gate_library_busy(request.app)
    try:
        reg.start(force=False)
    except RuntimeError:
        raise HTTPException(
            status_code=409, detail="An artist-art job is already running"
        ) from None
    handle = request.app.state.beets_library
    _start(request.app, reg, handle.lib, force=False, artist=None)
    return reg.state()


@router.get("/artists/art/backfill", response_model=ArtistArtBackfillStatus)
async def get_artist_art_backfill_status(
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    return reg.state()


@router.post("/artists/art/backfill/stop", response_model=ArtistArtBackfillStatus)
async def stop_artist_art_backfill(
    reg: Annotated[ArtistArtBackfillRegistry, Depends(get_artist_art_backfill)],
) -> ArtistArtBackfillStatus:
    reg.request_stop()
    return reg.state()


def _start(
    app: object,
    reg: ArtistArtBackfillRegistry,
    lib: object,
    *,
    force: bool,
    artist: str | None,
) -> None:
    from app.main import _resolve_cache_dir  # local import: same cache dir the service uses

    app_settings = getattr(app.state, "settings", None) or _module_settings  # type: ignore[attr-defined]
    delay = float(getattr(app_settings, "lyrics_backfill_delay_seconds", 0.2))
    start_art_backfill(
        reg,
        lib,
        cache_dir=_resolve_cache_dir(),
        settings=app_settings,
        delay=delay,
        force=force,
        artist=artist,
    )
