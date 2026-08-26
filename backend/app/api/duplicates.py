"""Duplicate Albums router — find duplicates + resolve a group.

Beets-free (CLAUDE.md rule 3): delegates entirely to ``app.beets.duplicates``.
The GET is synchronous + read-only; the POST is the mutating, serialized op.
"""

from typing import Final

from fastapi import APIRouter, Request

from app.beets.duplicates import (
    find_duplicate_albums,
    resolve_all_op,
    resolve_duplicates_op,
)
from app.beets.library import LibraryHandle
from app.events.emit import emit_library_changed
from app.models.duplicates import (
    DuplicateMode,
    DuplicatesReport,
    ResolveAllRequest,
    ResolveAllResult,
    ResolveRequest,
    ResolveResult,
)
from app.models.errors import ErrorDetail, StructuredErrorDetail

router = APIRouter(tags=["duplicates"])

#: Every refusal below is raised inside ``app/beets/duplicates.py``'s ops, never
#: in the two-line endpoints - which is why this router had no ``responses=`` at
#: all. Each entry names a model so the body keeps a generated type; the 500 is
#: the NESTED ``{message, recovery}`` shape, so it must NOT use ErrorDetail (see
#: app/models/errors.py).
_RESOLVE_ALBUM_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "One of the referenced album ids is no longer in the library.",
}
_RESOLVE_FAILED_RESPONSE: Final = {
    "model": StructuredErrorDetail,
    "description": (
        "The resolve failed part-way through moving copies to the Trash; the body"
        " carries the cause and a recovery hint."
    ),
}


@router.get("/duplicates")
def get_duplicates(
    request: Request, mode: DuplicateMode = DuplicateMode.strict
) -> DuplicatesReport:
    handle: LibraryHandle = request.app.state.beets_library
    return find_duplicate_albums(handle.lib, mode=mode)


@router.post(
    "/duplicates/resolve",
    responses={
        404: _RESOLVE_ALBUM_NOT_FOUND_RESPONSE,
        # TWO causes here, unlike the batch route below: the busy gate, and a
        # group whose membership changed since the report was taken
        # (``StaleGroupError``). The gate is ``library_job_active()`` with no
        # exclusions - the whole job union, not just imports, whatever the
        # detail sentence says. It does NOT include the beets swap lock: these
        # ops WAIT on that lock rather than refusing.
        409: {
            "model": ErrorDetail,
            "description": (
                "A library job (an import or a backfill) is in progress, or the"
                " duplicate group changed since the report was generated."
            ),
        },
        500: _RESOLVE_FAILED_RESPONSE,
    },
)
async def resolve_duplicates(req: ResolveRequest, request: Request) -> ResolveResult:
    result = await resolve_duplicates_op(request, req)
    emit_library_changed(request.app)
    return result


@router.post(
    "/duplicates/resolve-all",
    responses={
        404: _RESOLVE_ALBUM_NOT_FOUND_RESPONSE,
        # A stale group is skipped per-group inside ``resolve_all_groups`` and
        # reported in a 200 body, so the import gate is this route's ONLY 409.
        409: {
            "model": ErrorDetail,
            "description": (
                "A library job (an import or a backfill) is in progress, so the"
                " batch resolve is refused."
            ),
        },
        500: _RESOLVE_FAILED_RESPONSE,
    },
)
async def resolve_all_duplicates(req: ResolveAllRequest, request: Request) -> ResolveAllResult:
    result = await resolve_all_op(request, req)
    emit_library_changed(request.app)
    return result
