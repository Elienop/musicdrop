from __future__ import annotations

import asyncio

import pytest

from app.events.broker import MAX_QUEUE, EventBroker
from app.models.events import LibraryChangedEvent


def test_event_serializes_to_library_changed() -> None:
    assert LibraryChangedEvent().model_dump_json() == '{"type":"library:changed"}'


@pytest.mark.anyio
async def test_publish_library_changed_fans_out_to_all_subscribers() -> None:
    broker = EventBroker(asyncio.get_running_loop())
    a, b = broker.subscribe(), broker.subscribe()
    broker.publish_library_changed()
    assert await asyncio.wait_for(a.get(), 1.0) == '{"type":"library:changed"}'
    assert await asyncio.wait_for(b.get(), 1.0) == '{"type":"library:changed"}'


@pytest.mark.anyio
async def test_publish_art_changed_fans_out_art_changed() -> None:
    broker = EventBroker(asyncio.get_running_loop())
    a, b = broker.subscribe(), broker.subscribe()
    broker.publish_art_changed()
    assert await asyncio.wait_for(a.get(), 1.0) == '{"type":"art:changed"}'
    assert await asyncio.wait_for(b.get(), 1.0) == '{"type":"art:changed"}'


@pytest.mark.anyio
async def test_unsubscribe_stops_delivery() -> None:
    broker = EventBroker(asyncio.get_running_loop())
    q = broker.subscribe()
    assert broker.subscriber_count == 1
    broker.unsubscribe(q)
    assert broker.subscriber_count == 0
    broker.publish("x")
    await asyncio.sleep(0)
    assert q.empty()


@pytest.mark.anyio
async def test_full_queue_drops_without_raising() -> None:
    broker = EventBroker(asyncio.get_running_loop())
    q = broker.subscribe()
    for _ in range(MAX_QUEUE + 5):
        broker.publish("x")
    await asyncio.sleep(0)
    assert q.qsize() == MAX_QUEUE  # extra events dropped, no exception


@pytest.mark.anyio
async def test_publish_from_worker_thread_arrives_on_loop() -> None:
    broker = EventBroker(asyncio.get_running_loop())
    q = broker.subscribe()
    await asyncio.to_thread(broker.publish, "from-thread")  # not the loop thread
    assert await asyncio.wait_for(q.get(), 1.0) == "from-thread"


def test_publish_after_loop_closed_does_not_raise() -> None:
    # A daemon worker can finish AFTER the loop is closed at shutdown;
    # call_soon_threadsafe would then raise RuntimeError on the worker thread.
    loop = asyncio.new_event_loop()
    broker = EventBroker(loop)
    loop.close()
    broker.publish("x")  # loop closed/stopped at shutdown — dropped, no raise
