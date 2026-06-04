from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile

from app.api.albums import get_library
from app.artwork.cache import ArtistImageCache
from app.artwork.images import MAX_IMAGE_BYTES, sniff_image_mime
from app.artwork.service import ArtistImageService
from app.artwork.toggle import ArtistImageToggle
from app.beets.library import LibraryHandle, list_artists
from app.models.artist import Artist, ArtistImageOverrideResult, ArtistImageSettings

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
) -> Response:
    # Query param (not path) so names containing "/" (e.g. "AC/DC") work without
    # the %2F-in-path footgun.
    result = await service.get_artist_image(name)
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
