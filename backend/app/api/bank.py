"""The import-bank API — thin router over ``app.bank.store``.

The bank dir resolves from ``settings`` (not ``app.state``) so the
lifespan-less ``client`` test fixture works, same as the playlists router.
Store calls run in the threadpool: row I/O is tiny but the listing walks the
whole dir, and the event loop never blocks on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.bank import store
from app.beets.duplicates import find_import_duplicates
from app.beets.library import LibraryHandle
from app.config import settings
from app.models.bank import (
    BankBulkDeleteRequest,
    BankBulkDeleteResponse,
    BankBulkIgnoreRequest,
    BankBulkIgnoreResponse,
    BankDecision,
    BankDuplicatesResponse,
    BankItem,
    BankListResponse,
    BankReason,
    BankStatus,
)

router = APIRouter(tags=["bank"])


def get_bank_dir() -> Path:
    """Empty ``MUSICDROP_BANK_DIR`` -> ``<beets_dir>/bank``."""
    configured = settings.bank_dir.strip()
    if configured:
        return Path(configured)
    return Path(settings.beets_dir) / "bank"


@router.get("/bank", response_model=BankListResponse)
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


@router.get("/bank/{item_id}", response_model=BankItem)
async def get_bank_item(item_id: str) -> BankItem:
    item = await run_in_threadpool(store.get_item, get_bank_dir(), item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Bank item not found")
    return item


@router.get("/bank/{item_id}/duplicates", response_model=BankDuplicatesResponse)
async def bank_item_duplicates(
    item_id: str,
    request: Request,
    candidate_index: Annotated[int, Query(ge=0)] = 0,
) -> BankDuplicatesResponse:
    """Library albums the selected candidate would collide with — run beets'
    own duplicate query on the matched-release metadata (lazy, fresh)."""
    item = await run_in_threadpool(store.get_item, get_bank_dir(), item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Bank item not found")
    parked = item.parked
    if parked is None:
        return BankDuplicatesResponse(existing=[])  # nothing to check (no_match)
    after = parked.candidate.album_after
    options = parked.candidate.options
    idx = candidate_index if 0 <= candidate_index < len(options) else 0
    # Detect against the SELECTED option's own metadata so the up-front check
    # equals what the apply does (the apply pins this option's release_id and
    # beets runs find_duplicates on ITS albumartist/album). Fall back to
    # album_after field-by-field for legacy rows banked before options carried
    # their own identity (and for the top option, which equals album_after).
    opt = options[idx] if options else None
    albumartist = opt.album_artist if opt and opt.album_artist is not None else after.artist
    album = opt.album if opt and opt.album is not None else after.album
    year = opt.year if opt and opt.year is not None else after.year
    mb_albumid = opt.release_id if opt else None
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
    return BankDuplicatesResponse(existing=existing)


@router.post("/bank/{item_id}/decision", response_model=BankItem)
async def decide_bank_item(item_id: str, decision: BankDecision, request: Request) -> BankItem:
    try:
        item = await run_in_threadpool(store.decide_item, get_bank_dir(), item_id, decision)
    except store.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if item is None:
        raise HTTPException(status_code=404, detail="Bank item not found")
    if item.status == "queued":
        # Best-effort wake of the apply runner so the apply starts promptly;
        # its periodic poll is the correctness mechanism (same getattr posture
        # as the acquisition router - the lifespan-less test client has no
        # runner on app.state and must not care).
        runner = getattr(request.app.state, "bank_apply_runner", None)
        if runner is not None:
            runner.poke()
    return item


@router.delete("/bank/{item_id}", status_code=204)
async def delete_bank_item(item_id: str) -> Response:
    try:
        deleted = await run_in_threadpool(store.delete_item, get_bank_dir(), item_id)
    except store.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if not deleted:
        raise HTTPException(status_code=404, detail="Bank item not found")
    return Response(status_code=204)


@router.post("/bank/bulk-ignore", response_model=BankBulkIgnoreResponse)
async def bulk_ignore_bank(body: BankBulkIgnoreRequest) -> BankBulkIgnoreResponse:
    ignored = await run_in_threadpool(store.bulk_ignore, get_bank_dir(), body.ids)
    return BankBulkIgnoreResponse(ignored=ignored)


@router.post("/bank/bulk-delete", response_model=BankBulkDeleteResponse)
async def bulk_delete_bank(body: BankBulkDeleteRequest) -> BankBulkDeleteResponse:
    deleted = await run_in_threadpool(store.bulk_delete, get_bank_dir(), body.ids)
    return BankBulkDeleteResponse(deleted=deleted)
