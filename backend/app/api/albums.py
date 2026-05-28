from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.beets.library import LibraryHandle, get_album_cover, get_album_detail, list_albums
from app.models.album import Album, AlbumDetail, AlbumPage

router = APIRouter(tags=["albums"])


def get_library(request: Request) -> LibraryHandle:
    """Return the process-wide beets library handle opened at startup.

    The library is resolved once in the app lifespan and stored on
    ``app.state.beets_library`` — no per-request open. ``setup_beets`` always
    returns a handle (BEETSDIR + ``config.yaml`` are created from the starter
    if missing), so this dependency is non-optional. Tests override it to
    inject a hermetic temp handle (the override bypasses ``app.state``).
    """
    handle: LibraryHandle = request.app.state.beets_library
    return handle


@router.get("/albums", response_model=AlbumPage)
async def list_albums_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    artist: Annotated[str | None, Query()] = None,
) -> AlbumPage:
    items: list[Album]
    items, total = list_albums(handle.lib, limit=limit, offset=offset, artist=artist)
    return AlbumPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/albums/{album_id}", response_model=AlbumDetail)
async def get_album_detail_endpoint(
    album_id: int,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> AlbumDetail:
    detail = get_album_detail(handle.lib, album_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Album not found")
    return detail


@router.get("/albums/{album_id}/cover")
async def get_album_cover_endpoint(
    album_id: int,
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> Response:
    cover = get_album_cover(handle.lib, album_id)
    if cover is None:
        raise HTTPException(status_code=404, detail="Cover not found")

    image_bytes, mime = cover
    return Response(
        content=image_bytes,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=3600"},
    )
