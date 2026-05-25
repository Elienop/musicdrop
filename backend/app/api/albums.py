import os
from typing import Annotated

from beets.library import Library
from fastapi import APIRouter, Depends, Query

from app.beets.library import list_albums, open_library
from app.config import settings
from app.models.album import Album, AlbumPage

router = APIRouter(tags=["albums"])


def get_library() -> Library | None:
    """Resolve the beets library from settings.

    Returns ``None`` when no library path is configured or the file is missing,
    so the endpoint can degrade to an empty page instead of crashing. Overridden
    in tests to inject a hermetic temp library.
    """
    path = settings.beets_library_path
    if not path or not os.path.exists(path):
        return None
    return open_library(path, settings.beets_library_directory)


@router.get("/albums", response_model=AlbumPage)
async def list_albums_endpoint(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    lib: Annotated[Library | None, Depends(get_library)] = None,
) -> AlbumPage:
    if lib is None:
        return AlbumPage(items=[], total=0, limit=limit, offset=offset)

    items: list[Album]
    items, total = list_albums(lib, limit=limit, offset=offset)
    return AlbumPage(items=items, total=total, limit=limit, offset=offset)
