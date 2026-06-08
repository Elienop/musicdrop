"""The acquisition status endpoint.

A single read-only probe over the lifespan-constructed ``AcquisitionQueue``.
Status is informational only (the queue is not a mutex participant — Option A).
Under the lifespan-less test client there is no queue on ``app.state``, so the
endpoint falls back to an idle status rather than 500 — it is polled frequently.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.models.acquisition import AcquisitionQueueStatus

router = APIRouter(tags=["acquisition"])


@router.get("/acquisition/status", response_model=AcquisitionQueueStatus)
async def get_acquisition_status(request: Request) -> AcquisitionQueueStatus:
    queue = getattr(request.app.state, "acquisition_queue", None)
    if queue is None:
        return AcquisitionQueueStatus(
            phase="idle",
            queued=0,
            current=None,
            processed=0,
            set_aside=0,
            failed=0,
            error=None,
        )
    status: AcquisitionQueueStatus = queue.status()
    return status
