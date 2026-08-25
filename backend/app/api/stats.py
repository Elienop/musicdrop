"""Read-only library stats endpoint for the home dashboard."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from app.api.albums import get_library
from app.beets.library import LibraryHandle
from app.beets.stats import build_stats_response
from app.models.stats import LibraryStatsResponse

router = APIRouter(tags=["stats"])


@router.get("/stats")
async def get_stats(
    handle: Annotated[LibraryHandle, Depends(get_library)],
) -> LibraryStatsResponse:
    """Library counts + recently-added albums. Pure read; one library scan,
    offloaded to the threadpool so the event loop stays free."""
    return await run_in_threadpool(build_stats_response, handle.lib)
