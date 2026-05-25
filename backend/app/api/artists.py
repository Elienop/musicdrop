from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.albums import get_library
from app.beets.library import LibraryHandle, list_artists
from app.models.artist import Artist

router = APIRouter(tags=["artists"])


@router.get("/artists", response_model=list[Artist])
async def list_artists_endpoint(
    lib: Annotated[LibraryHandle | None, Depends(get_library)] = None,
) -> list[Artist]:
    if lib is None:
        return []
    return list_artists(lib)
