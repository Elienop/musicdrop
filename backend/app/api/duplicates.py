"""Duplicate Albums router — find duplicates + resolve a group.

Beets-free (CLAUDE.md rule 3): delegates entirely to ``app.beets.duplicates``.
The GET is synchronous + read-only; the POST is the mutating, serialized op.
"""

from fastapi import APIRouter, Request

from app.beets.duplicates import (
    find_duplicate_albums,
    resolve_all_op,
    resolve_duplicates_op,
)
from app.beets.library import LibraryHandle
from app.models.duplicates import (
    DuplicateMode,
    DuplicatesReport,
    ResolveAllRequest,
    ResolveAllResult,
    ResolveRequest,
    ResolveResult,
)

router = APIRouter(tags=["duplicates"])


@router.get("/duplicates", response_model=DuplicatesReport)
def get_duplicates(
    request: Request, mode: DuplicateMode = DuplicateMode.strict
) -> DuplicatesReport:
    handle: LibraryHandle = request.app.state.beets_library
    return find_duplicate_albums(handle.lib, mode=mode)


@router.post("/duplicates/resolve", response_model=ResolveResult)
async def resolve_duplicates(req: ResolveRequest, request: Request) -> ResolveResult:
    return await resolve_duplicates_op(request, req)


@router.post("/duplicates/resolve-all", response_model=ResolveAllResult)
async def resolve_all_duplicates(req: ResolveAllRequest, request: Request) -> ResolveAllResult:
    return await resolve_all_op(request, req)
