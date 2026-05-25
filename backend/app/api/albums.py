from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.beets.library import LibraryHandle, list_albums
from app.models.album import Album, AlbumPage

router = APIRouter(tags=["albums"])


def get_library(request: Request) -> LibraryHandle | None:
    """Return the process-wide beets library opened at startup (or None).

    The library is resolved once in the app lifespan and stored on
    ``app.state.beets_library`` — no per-request open. Overridden in tests to
    inject a hermetic temp library (the override bypasses app.state entirely).
    """
    lib: LibraryHandle | None = getattr(request.app.state, "beets_library", None)
    return lib


@router.get("/albums", response_model=AlbumPage)
async def list_albums_endpoint(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    lib: Annotated[LibraryHandle | None, Depends(get_library)] = None,
) -> AlbumPage:
    if lib is None:
        return AlbumPage(items=[], total=0, limit=limit, offset=offset)

    items: list[Album]
    items, total = list_albums(lib, limit=limit, offset=offset)
    return AlbumPage(items=items, total=total, limit=limit, offset=offset)
