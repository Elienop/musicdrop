"""The import-bank API — thin router over ``app.bank.store``.

The bank dir resolves from ``settings`` (not ``app.state``) so the
lifespan-less ``client`` test fixture works, same as the playlists router.
Store calls run in the threadpool: row I/O is tiny but the listing walks the
whole dir, and the event loop never blocks on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.api.candidate_identity import selected_option_identity
from app.bank import store
from app.bank.apply_runner import _STALE_CHANGED_ERROR, _STALE_GONE_ERROR
from app.bank.fingerprint import folder_fingerprint
from app.beets.duplicates import find_import_duplicates
from app.beets.library import LibraryHandle
from app.beets.research import NoAudioFilesError, rescan_folder, research_folder
from app.config import BANK_STORE, settings, store_dir
from app.import_jobs.runner import SourcePathMissingError, refuse_unless_absent
from app.models.bank import (
    BankBulkDeleteRequest,
    BankBulkDeleteResponse,
    BankBulkIgnoreRequest,
    BankBulkIgnoreResponse,
    BankDecision,
    BankItem,
    BankListResponse,
    BankReason,
    BankSearchResponse,
    BankStatus,
)
from app.models.errors import ErrorDetail
from app.models.import_models import DuplicatesCheckResponse, ImportSearch, ParkedAlbum

_BANK_ITEM_NOT_FOUND = "Bank item not found"

#: The OpenAPI entry for a route that 404s ONLY because the named bank item does
#: not exist (see app/models/errors.py for why the model must be named). Named
#: because all six item routes share this single cause and must not drift apart.
_BANK_NOT_FOUND_RESPONSE: Final = {
    "model": ErrorDetail,
    "description": "The bank item does not exist.",
}

router = APIRouter(tags=["bank"])


def get_bank_dir() -> Path:
    """Empty ``MUSICDROP_BANK_DIR`` -> ``<beets_dir>/bank``."""
    return store_dir(settings, BANK_STORE, Path(settings.beets_dir))


def _current_fingerprint(folder: str) -> str | None:
    """The banked folder's fingerprint as it is now, or ``None`` if it is gone.

    ``FileNotFoundError`` is ``folder_fingerprint``'s own "gone" signal, raised
    by hand with no errno - an errno test would miss it. Catching that one
    ALONE let a PermissionError escape as a 500, so every other ``OSError``
    goes to ``refuse_unless_absent``, which returns for an absent errno and
    otherwise raises the shared sentence. Not ``str(exc)``, which on an OSError
    interpolates ``exc.filename`` - an absolute server path - into the body.

    A folder the OS refuses to answer for did NOT go stale, which is why that
    arm raises instead of answering ``None``. The two callers read ``None``
    differently: search flips the row stale, rescan answers 409.
    """
    try:
        return folder_fingerprint(Path(folder))
    except FileNotFoundError:
        return None
    except OSError as exc:
        refuse_unless_absent(exc)
        return None


@router.get("/bank")
async def list_bank(
    status_filter: Annotated[BankStatus | None, Query(alias="status")] = None,
    view: Annotated[Literal["all", "active"], Query()] = "all",
    reason: Annotated[BankReason | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> BankListResponse:
    bank_dir = get_bank_dir()
    # A specific status wins; ``view=active`` only narrows the unfiltered list
    # to the Review page's needs-attention statuses (the FE never sends both).
    # ``reason`` ANDs on top. One index pass yields the page, the filtered
    # total, and total_all (any status) so the Review page section stays visible
    # once the active view empties.
    active_only = status_filter is None and view == "active"
    page, total, total_all = await run_in_threadpool(
        lambda: store.list_page(
            bank_dir,
            status=status_filter,
            active_only=active_only,
            reason=reason,
            offset=offset,
            limit=limit,
        )
    )
    return BankListResponse(
        items=page, total=total, total_all=total_all, offset=offset, limit=limit
    )


@router.get("/bank/{item_id}", responses={404: _BANK_NOT_FOUND_RESPONSE})
async def get_bank_item(item_id: str) -> BankItem:
    item = await run_in_threadpool(store.get_item, get_bank_dir(), item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_BANK_ITEM_NOT_FOUND)
    return item


@router.get(
    "/bank/{item_id}/duplicates",
    responses={404: _BANK_NOT_FOUND_RESPONSE},
)
async def bank_item_duplicates(
    item_id: str,
    request: Request,
    candidate_index: Annotated[int, Query(ge=0)] = 0,
) -> DuplicatesCheckResponse:
    """Library albums the selected candidate would collide with — run beets'
    own duplicate query on the matched-release metadata (lazy, fresh)."""
    item = await run_in_threadpool(store.get_item, get_bank_dir(), item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_BANK_ITEM_NOT_FOUND)
    parked = item.parked
    if parked is None or item.reason == "needs_dup_resolution":
        # Nothing to check: a no_match row has no candidate at all, and a
        # dup row's collision is the prompt it was banked WITH (item.duplicate,
        # what the duplicate screen renders and what its four actions resolve).
        # Such a row carries a parked payload only to PIN its apply to the
        # matched release - re-checking that payload here would answer a
        # question this row's screen never asks, from a second source that can
        # disagree with the banked prompt. Gated on the reason, like the search
        # endpoint below.
        return DuplicatesCheckResponse(existing=[])
    albumartist, album, year, mb_albumid = selected_option_identity(
        parked.candidate.options, candidate_index, parked.candidate.album_after
    )
    handle: LibraryHandle = request.app.state.beets_library
    existing = await run_in_threadpool(
        find_import_duplicates,
        handle.lib,
        albumartist=albumartist,
        album=album,
        year=year,
        mb_albumid=mb_albumid,
        exclude_under=item.folder,
    )
    return DuplicatesCheckResponse(existing=existing)


@router.post(
    "/bank/{item_id}/search",
    responses={
        404: _BANK_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": (
                "The row is not an undecided match row, so the search was refused"
                " (it is already decided, or its folder went stale or cannot be"
                " read)."
            ),
        },
    },
)
async def search_bank_item(item_id: str, search: ImportSearch) -> BankSearchResponse:
    """Re-look-up a banked folder against a release id/URL or a name search.

    Preview-only (the attended flow's "enter Id" rescue, run offline): reads
    the folder's tags, queries the metadata sources, and replaces the row's
    candidate payload. Never touches the import slot or the library.
    """
    bank_dir = get_bank_dir()
    item = await run_in_threadpool(store.get_item, bank_dir, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_BANK_ITEM_NOT_FOUND)
    # Fast-path pre-check only — the store's locked _SEARCHABLE re-check in
    # research_item is authoritative if this tuple ever drifts.
    if item.status not in ("needs_review", "failed") or item.reason == "needs_dup_resolution":
        raise HTTPException(
            status_code=409,
            detail=f"row is {item.status}/{item.reason}; a search needs an undecided match row",
        )

    # A searched payload must describe the banked files: a changed or vanished
    # folder flips to stale (the apply runner's exact semantics) instead.
    # The flip below is intentionally unguarded (no expected=): the mismatch is
    # a fact about the disk, and a decision racing past the pre-check would hit
    # the apply runner's own fingerprint re-check and land on stale anyway.
    try:
        current = await run_in_threadpool(_current_fingerprint, item.folder)
    except SourcePathMissingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if current is None or current != item.fingerprint:
        error = _STALE_GONE_ERROR if current is None else _STALE_CHANGED_ERROR
        await run_in_threadpool(lambda: store.set_status(bank_dir, item_id, "stale", error=error))
        raise HTTPException(status_code=409, detail=error)

    result = await run_in_threadpool(research_folder, item.folder, search)
    if result is None:
        return BankSearchResponse(item=item, found=False)
    previous_revision = item.parked.candidate.search_revision if item.parked else 0
    parked = ParkedAlbum(
        album_index=item.parked.album_index if item.parked else 0,
        folder=item.folder,
        candidate=result.candidate.model_copy(update={"search_revision": previous_revision + 1}),
    )
    try:
        updated = await run_in_threadpool(
            lambda: store.research_item(
                bank_dir,
                item_id,
                parked=parked,
                artist=result.artist,
                album=result.album,
                recommendation=result.recommendation.value,
                confidence=result.confidence,
            )
        )
    except store.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail=_BANK_ITEM_NOT_FOUND)
    return BankSearchResponse(item=updated, found=True)


@router.post(
    "/bank/{item_id}/rescan",
    responses={
        404: _BANK_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": (
                "The row cannot be rescanned (already decided, its folder is gone"
                " or cannot be read, or it holds no audio files)."
            ),
        },
    },
)
async def rescan_bank_item(item_id: str) -> BankItem:
    """Re-read the banked folder from disk and re-match it in place.

    The explicit "I changed the folder on purpose" gesture (deleted a
    duplicate track, added a missing one): re-reads tags, runs beets' DEFAULT
    first-scan lookup, and REFRESHES the fingerprint — the deliberate
    contrast with search, which treats a changed folder as stale. Also the
    stale row's in-place rescue. Preview-only: no file or library writes.
    """
    bank_dir = get_bank_dir()
    item = await run_in_threadpool(store.get_item, bank_dir, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=_BANK_ITEM_NOT_FOUND)
    if item.status not in ("needs_review", "failed", "stale"):
        raise HTTPException(
            status_code=409,
            detail=f"row is {item.status}; a rescan needs an undecided row",
        )

    # Fingerprint FIRST: it describes the folder version being blessed. An
    # edit racing the lookup below surfaces as a mismatch at apply time and
    # goes stale — the safe direction.
    try:
        fingerprint = await run_in_threadpool(_current_fingerprint, item.folder)
    except SourcePathMissingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if fingerprint is None:
        raise HTTPException(
            status_code=409, detail="the banked folder no longer exists; remove the row"
        )
    try:
        outcome = await run_in_threadpool(rescan_folder, item.folder)
    except NoAudioFilesError:
        raise HTTPException(
            status_code=409,
            detail="no audio files remain in the folder; remove the row or restore files",
        ) from None

    previous_revision = item.parked.candidate.search_revision if item.parked else 0
    if outcome.result is not None:
        parked: ParkedAlbum | None = ParkedAlbum(
            album_index=item.parked.album_index if item.parked else 0,
            folder=item.folder,
            candidate=outcome.result.candidate.model_copy(
                update={"search_revision": previous_revision + 1}
            ),
        )
        artist, album = outcome.result.artist, outcome.result.album
        recommendation = outcome.result.recommendation.value
        confidence: float | None = outcome.result.confidence
    else:
        parked = None
        artist, album = outcome.cur_artist, outcome.cur_album
        recommendation = outcome.recommendation.value
        confidence = 0.0

    try:
        updated = await run_in_threadpool(
            lambda: store.rescan_item(
                bank_dir,
                item_id,
                fingerprint=fingerprint,
                parked=parked,
                artist=artist,
                album=album,
                recommendation=recommendation,
                confidence=confidence,
            )
        )
    except store.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if updated is None:
        raise HTTPException(status_code=404, detail=_BANK_ITEM_NOT_FOUND)
    return updated


@router.post(
    "/bank/{item_id}/decision",
    responses={
        404: _BANK_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": "The row's state changed, so the decision is no longer valid.",
        },
    },
)
async def decide_bank_item(item_id: str, decision: BankDecision, request: Request) -> BankItem:
    try:
        item = await run_in_threadpool(store.decide_item, get_bank_dir(), item_id, decision)
    except store.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if item is None:
        raise HTTPException(status_code=404, detail=_BANK_ITEM_NOT_FOUND)
    if item.status == "queued":
        # Best-effort wake of the apply runner so the apply starts promptly;
        # its periodic poll is the correctness mechanism (same getattr posture
        # as the acquisition router - the lifespan-less test client has no
        # runner on app.state and must not care).
        runner = getattr(request.app.state, "bank_apply_runner", None)
        if runner is not None:
            runner.poke()
    return item


@router.delete(
    "/bank/{item_id}",
    status_code=204,
    responses={
        404: _BANK_NOT_FOUND_RESPONSE,
        409: {
            "model": ErrorDetail,
            "description": "The row's state changed, so it can no longer be deleted.",
        },
    },
)
async def delete_bank_item(item_id: str) -> Response:
    try:
        deleted = await run_in_threadpool(store.delete_item, get_bank_dir(), item_id)
    except store.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if not deleted:
        raise HTTPException(status_code=404, detail=_BANK_ITEM_NOT_FOUND)
    return Response(status_code=204)


@router.post("/bank/bulk-ignore")
async def bulk_ignore_bank(body: BankBulkIgnoreRequest) -> BankBulkIgnoreResponse:
    ignored = await run_in_threadpool(store.bulk_ignore, get_bank_dir(), body.ids)
    return BankBulkIgnoreResponse(ignored=ignored)


@router.post("/bank/bulk-delete")
async def bulk_delete_bank(body: BankBulkDeleteRequest) -> BankBulkDeleteResponse:
    deleted = await run_in_threadpool(store.bulk_delete, get_bank_dir(), body.ids)
    return BankBulkDeleteResponse(deleted=deleted)
