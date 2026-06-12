"""The import-bank API — thin router over ``app.bank.store``.

The bank dir resolves from ``settings`` (not ``app.state``) so the
lifespan-less ``client`` test fixture works, same as the playlists router.
Store calls run in the threadpool: row I/O is tiny but the listing walks the
whole dir, and the event loop never blocks on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from app.bank import store
from app.config import settings
from app.models.bank import (
    BankBulkIgnoreRequest,
    BankBulkIgnoreResponse,
    BankDecision,
    BankItem,
    BankListResponse,
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
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> BankListResponse:
    bank_dir = get_bank_dir()
    items = await run_in_threadpool(
        lambda: store.list_items(bank_dir, status=status_filter, offset=offset, limit=limit)
    )
    total = await run_in_threadpool(lambda: store.count_items(bank_dir, status=status_filter))
    return BankListResponse(items=items, total=total, offset=offset, limit=limit)


@router.get("/bank/{item_id}", response_model=BankItem)
async def get_bank_item(item_id: str) -> BankItem:
    item = await run_in_threadpool(store.get_item, get_bank_dir(), item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Bank item not found")
    return item


@router.post("/bank/{item_id}/decision", response_model=BankItem)
async def decide_bank_item(item_id: str, decision: BankDecision) -> BankItem:
    try:
        item = await run_in_threadpool(store.decide_item, get_bank_dir(), item_id, decision)
    except store.InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if item is None:
        raise HTTPException(status_code=404, detail="Bank item not found")
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
