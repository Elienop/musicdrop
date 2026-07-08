"""Faceted Browse endpoints — facet values + the filtered album page.

Read-only; both delegate to the beets adapter (rule 3). Filter params repeat
(``?genre=Rock&genre=Metal``) so values can contain any character without a
separator collision; an empty/absent facet imposes no constraint.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.beets.browse import browse_albums, browse_facets
from app.beets.library import LibraryHandle
from app.models.album import AlbumPage
from app.models.browse import BrowseFacets

router = APIRouter(tags=["browse"])


@router.get("/browse/facets", response_model=BrowseFacets)
async def browse_facets_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> BrowseFacets:
    return await run_in_threadpool(browse_facets, handle.lib)


@router.get("/browse/albums", response_model=AlbumPage)
async def browse_albums_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
    genre: Annotated[list[str] | None, Query()] = None,
    decade: Annotated[list[str] | None, Query()] = None,
    format: Annotated[list[str] | None, Query()] = None,
    album_type: Annotated[list[str] | None, Query()] = None,
    source: Annotated[list[str] | None, Query()] = None,
    media: Annotated[list[str] | None, Query()] = None,
    country: Annotated[list[str] | None, Query()] = None,
    lyrics: Annotated[list[str] | None, Query()] = None,
    tracks: Annotated[list[str] | None, Query()] = None,
    sort: Annotated[Literal["artist", "added"], Query()] = "artist",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlbumPage:
    items, total = await run_in_threadpool(
        browse_albums,
        handle.lib,
        genres=genre or [],
        decades=decade or [],
        formats=format or [],
        album_types=album_type or [],
        sources=source or [],
        medias=media or [],
        countries=country or [],
        lyrics=lyrics or [],
        tracks=tracks or [],
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return AlbumPage(items=items, total=total, limit=limit, offset=offset)
