"""Duplicate Albums router — find duplicates + resolve a group.

Beets-free (CLAUDE.md rule 3): delegates entirely to ``app.beets.duplicates``.
The GET is synchronous + read-only; the POST is the mutating, serialized op.
"""

from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request

from app.api.albums import get_library
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
from app.playlists.reexport import reexport_playlists_containing
from app.playlists.store import get_playlists_dir

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
#: Both ops resolve the Trash / origin-store pair per request and run the
#: containment check on what it resolved to, before the first copy moves.
_RESOLVE_LAYOUT_REFUSED_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": (
        "The store layout is refused, so no copies were moved; the message names"
        " the setting and both resolved paths."
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
        503: _RESOLVE_LAYOUT_REFUSED_RESPONSE,
    },
)
async def resolve_duplicates(
    req: ResolveRequest,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> ResolveResult:
    """Resolve one group, then re-export the `.m3u8` of every playlist that held a
    track from a loser album — those exports now name files that live in Trash.

    Before ``emit_library_changed`` on purpose: the event tells open tabs to
    refetch, so the exports should already be repaired when they do.
    """
    dropped_ids: set[int] = set()
    result = await resolve_duplicates_op(request, req, dropped_ids)
    reexported = await reexport_playlists_containing(dropped_ids, handle, playlists_dir)
    emit_library_changed(request.app)
    return result.model_copy(update={"playlists_reexported": reexported})


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
        503: _RESOLVE_LAYOUT_REFUSED_RESPONSE,
    },
)
async def resolve_all_duplicates(
    req: ResolveAllRequest,
    request: Request,
    handle: Annotated[LibraryHandle, Depends(get_library)],
    playlists_dir: Annotated[Path, Depends(get_playlists_dir)],
) -> ResolveAllResult:
    """Batch resolve + the same `.m3u8` collateral, run ONCE over the union of
    every group's dropped items rather than per group: a playlist holding tracks
    from two groups must be rewritten once and counted once."""
    dropped_ids: set[int] = set()
    result = await resolve_all_op(request, req, dropped_ids)
    reexported = await reexport_playlists_containing(dropped_ids, handle, playlists_dir)
    emit_library_changed(request.app)
    return result.model_copy(update={"playlists_reexported": reexported})
