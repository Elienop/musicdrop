from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.albums import get_library
from app.beets.library import LibraryHandle, search
from app.models.search import SearchResults

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResults)
async def search_endpoint(
    handle: Annotated[LibraryHandle, Depends(get_library)],
    # Empty q is allowed (no min_length): a blank term returns empty results
    # rather than a 422, so the FE can keep the query in the URL while idle.
    q: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> SearchResults:
    return search(handle.lib, query=q, limit=limit)
