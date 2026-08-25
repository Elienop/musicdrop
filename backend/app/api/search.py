from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.beets.library import LibraryHandle, search, search_typed
from app.models.search import SearchEntity, SearchResults, TypedSearchPage

router = APIRouter(tags=["search"])


@router.get("/search")
async def search_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
    # Empty q is allowed (no min_length): a blank term returns empty results
    # rather than a 422, so the FE can keep the query in the URL while idle.
    q: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    # Typed ("View all") mode only; accepted-but-ignored without `type`, since
    # the sectioned response is not paged.
    offset: Annotated[int, Query(ge=0)] = 0,
    # `type_` because `type` shadows the builtin; the wire name stays `type`.
    type_: Annotated[SearchEntity | None, Query(alias="type")] = None,
) -> SearchResults | TypedSearchPage:
    if type_ is None:
        return await run_in_threadpool(search, handle.lib, query=q, limit=limit)
    return await run_in_threadpool(
        search_typed, handle.lib, query=q, entity=type_, limit=limit, offset=offset
    )
