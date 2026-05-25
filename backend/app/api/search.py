from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.albums import get_library
from app.beets.library import LibraryHandle, search
from app.models.search import SearchResults

router = APIRouter(tags=["search"])


def _empty_results() -> SearchResults:
    return SearchResults(
        artists=[], albums=[], tracks=[], artist_total=0, album_total=0, track_total=0
    )


@router.get("/search", response_model=SearchResults)
async def search_endpoint(
    # Empty q is allowed (no min_length): a blank term returns empty results
    # rather than a 422, so the FE can keep the query in the URL while idle.
    q: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    lib: Annotated[LibraryHandle | None, Depends(get_library)] = None,
) -> SearchResults:
    if lib is None:
        return _empty_results()
    return search(lib, query=q, limit=limit)
