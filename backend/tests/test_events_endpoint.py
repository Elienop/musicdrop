from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from starlette.responses import StreamingResponse

from app.api import events as events_module
from app.api.events import events_endpoint
from app.events.broker import EventBroker


def _request_with(broker: EventBroker) -> Request:
    # The endpoint only reads request.app.state.event_broker; a minimal stand-in
    # avoids httpx.ASGITransport, which buffers an infinite SSE body and hangs.
    return cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(event_broker=broker))),
    )


def _frames(response: StreamingResponse) -> AsyncGenerator[str, None]:
    # body_iterator is typed AsyncIterable (no __anext__/aclose); our stream() is
    # really an async generator, so cast to drive + close it directly.
    return cast("AsyncGenerator[str, None]", response.body_iterator)


@pytest.mark.anyio
async def test_stream_delivers_published_event() -> None:
    broker = EventBroker(asyncio.get_running_loop())
    response = await events_endpoint(_request_with(broker))
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"

    agen = _frames(response)
    broker.publish_library_changed()
    frame = await asyncio.wait_for(anext(agen), 1.0)
    assert frame == 'data: {"type":"library:changed"}\n\n'
    await agen.aclose()


@pytest.mark.anyio
async def test_heartbeat_on_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no event queued, the stream emits a comment heartbeat after the
    # timeout — shortened here so the test stays fast.
    monkeypatch.setattr(events_module, "HEARTBEAT_SECONDS", 0.01)
    broker = EventBroker(asyncio.get_running_loop())
    response = await events_endpoint(_request_with(broker))

    agen = _frames(response)
    frame = await asyncio.wait_for(anext(agen), 1.0)
    assert frame == ": ping\n\n"
    await agen.aclose()


@pytest.mark.anyio
async def test_disconnect_unsubscribes() -> None:
    broker = EventBroker(asyncio.get_running_loop())
    response = await events_endpoint(_request_with(broker))
    assert broker.subscriber_count == 1  # subscribed for the duration of the stream

    agen = _frames(response)
    broker.publish_library_changed()
    await asyncio.wait_for(anext(agen), 1.0)  # advance into the loop's try block
    await agen.aclose()  # client disconnect closes the generator -> finally unsubscribes
    assert broker.subscriber_count == 0
