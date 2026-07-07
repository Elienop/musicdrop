"""Server-Sent Events stream: one connection per tab, fed by the EventBroker.

Hand-rolled SSE on StreamingResponse (no extra dependency). The client's
``onopen`` does the reconnect catch-up, so the server sends no initial event —
it just streams ``data:`` frames as changes happen, with a periodic ``: ping``
comment to keep idle reverse proxies from dropping the connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

from app.events.broker import MAX_SUBSCRIBERS, EventBroker
from app.models.events import LibraryChangedEvent

router = APIRouter(tags=["events"])

HEARTBEAT_SECONDS = 20.0  # < a typical nginx proxy_read_timeout (60s)


@router.get(
    "/events",
    responses={
        200: {"model": LibraryChangedEvent, "description": "SSE stream of library-change events."}
    },
)
async def events_endpoint(request: Request) -> StreamingResponse:
    broker: EventBroker | None = getattr(request.app.state, "event_broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail="Event stream not available")
    if broker.subscriber_count >= MAX_SUBSCRIBERS:
        raise HTTPException(status_code=503, detail="Too many active event streams")
    queue = broker.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"  # comment line; EventSource ignores it
                    continue
                yield f"data: {event}\n\n"
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
