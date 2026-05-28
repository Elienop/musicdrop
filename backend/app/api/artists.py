from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.albums import get_library
from app.artwork.service import ArtistImageService
from app.beets.library import LibraryHandle, list_artists
from app.models.artist import Artist

router = APIRouter(tags=["artists"])


def get_artist_image_service(request: Request) -> ArtistImageService:
    """Return the process-wide artist-image service built at startup.

    Constructed once in the app lifespan and stored on
    ``app.state.artist_image_service``. Overridden in tests to inject a stub so
    no real Deezer call is made.
    """
    service: ArtistImageService = request.app.state.artist_image_service
    return service


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
